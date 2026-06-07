#!/usr/bin/env python3
"""
SDN Adaptif QoS – Ryu Controller
Başlatmak: ryu-manager adaptive_controller.py --observe-links

OpenFlow 1.3 üzerinden:
  - L2 öğrenme switch (MAC tablosu)
  - QoS kuyrukları (HTB meter tabanlı)
  - REST API ile mod değiştirme: POST /qos/mode {"mode": "baseline"|"static"|"adaptive"}
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp
from ryu.app.wsgi import WSGIApplication, ControllerBase, route, Response
from ryu.lib import hub

import json
import time
import threading
import logging

LOG = logging.getLogger('adaptive_qos')

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------
IDLE_TIMEOUT  = 30      # akış tablosu boşta kalma süresi (sn)
HARD_TIMEOUT  = 0       # sonsuz
MONITOR_INTV  = 5       # istatistik toplama aralığı (sn)

# Öncelik değerleri (yüksek = öncelikli)
PRIO_HIGH   = 200
PRIO_MEDIUM = 100
PRIO_LOW    = 10
PRIO_DEFAULT = 1

# IP/Port tabanlı trafik sınıflandırma (örnek)
# UDP 5001 → VoIP, UDP 5002 → Video, TCP → Bulk
VOIP_DST_PORT  = 5001
VIDEO_DST_PORT = 5002

# Adaptif eşikler
VOIP_LOSS_THRESH  = 2.0   # %
VOIP_TPUT_MIN     = 0.3   # Mbps

# Meter ID'leri
METER_BULK_ID  = 1
METER_VIDEO_ID = 2


# ---------------------------------------------------------------------------
# REST API Kaynakları
# ---------------------------------------------------------------------------

class QoSAPI(ControllerBase):
    """Dışarıdan mod değiştirme ve durum sorgulama için REST API."""

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.ctrl = data['controller']

    @route('qos', '/qos/mode', methods=['GET'])
    def get_mode(self, req, **kwargs):
        body = json.dumps({
            'mode'        : self.ctrl.mode,
            'bulk_limit'  : self.ctrl.bulk_limit_kbps,
            'constrained' : self.ctrl.constrained,
        })
        return Response(content_type='application/json', body=body)

    @route('qos', '/qos/mode', methods=['POST'])
    def set_mode(self, req, **kwargs):
        try:
            data = json.loads(req.body)
            mode = data.get('mode', 'baseline')
            if mode not in ('baseline', 'static', 'adaptive'):
                return Response(status=400, content_type='application/json',
                                body='{"error":"invalid mode"}')
            bulk_limit = data.get('bulk_limit_mbps', 2)
            self.ctrl.set_mode(mode, bulk_limit_mbps=bulk_limit)
            return Response(content_type='application/json',
                            body=json.dumps({'ok': True, 'mode': mode}))
        except Exception as e:
            return Response(status=500, content_type='application/json',
                            body=json.dumps({'error': str(e)}))

    @route('qos', '/qos/stats', methods=['GET'])
    def get_stats(self, req, **kwargs):
        body = json.dumps(self.ctrl.last_stats)
        return Response(content_type='application/json', body=body)

    @route('qos', '/qos/stats/update', methods=['POST'])
    def update_stats(self, req, **kwargs):
        """topology.py'den gelen ölçüm sonuçlarını saklar."""
        try:
            data = json.loads(req.body)
            self.ctrl.last_stats = data
            return Response(content_type='application/json', body='{"ok":true}')
        except Exception as e:
            return Response(status=500, content_type='application/json',
                            body=json.dumps({'error': str(e)}))


# ---------------------------------------------------------------------------
# Ana Kontrolcü Uygulaması
# ---------------------------------------------------------------------------

class AdaptiveQoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS    = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        wsgi = kwargs['wsgi']
        wsgi.register(QoSAPI, {'controller': self})

        self.mac_to_port  = {}          # dpid → {mac: port}
        self.datapaths    = {}          # dpid → datapath
        self.mode         = 'baseline'  # mevcut mod
        self.bulk_limit_kbps = 2000     # statik mod Bulk kısıtı (kbps)
        self.constrained  = False       # adaptif mod kısıt durumu
        self.lock         = threading.Lock()

        # İstatistik geçmişi (dashboard için)
        self.last_stats = {
            'voip_loss'   : 0.0,
            'video_loss'  : 0.0,
            'bulk_tput'   : 0.0,
            'timestamp'   : 0,
        }

        # İzleme döngüsü
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # -----------------------------------------------------------------------
    # OpenFlow olayları
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp      = ev.msg.datapath
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser

        self.datapaths[dp.id] = dp
        LOG.info('Switch bağlandı: dpid=%s', dp.id)

        # Table-miss akışı: kontrolcüye gönder
        match  = parser.OFPMatch()
        action = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                         ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, action)

        # Başlangıç moduna göre kuralları kur
        self._apply_mode(dp)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg     = ev.msg
        dp      = msg.datapath
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst_mac = eth.dst
        src_mac = eth.src
        dpid    = dp.id

        # MAC tablosunu öğren
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port

        out_port = (self.mac_to_port[dpid].get(dst_mac)
                    or ofproto.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            # Kalıcı akış kuralı yaz
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac)
            self._add_flow(dp, PRIO_DEFAULT, match, actions,
                           idle_timeout=IDLE_TIMEOUT)

        # Paketi gönder
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out  = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        dp.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """Akış istatistiklerini alır, adaptif karar için kullanır."""
        body = ev.msg.body
        for stat in sorted(body, key=lambda s: s.priority, reverse=True):
            match = stat.match
            LOG.debug('Flow: %s pkt=%d byte=%d',
                      match, stat.packet_count, stat.byte_count)

    # -----------------------------------------------------------------------
    # Yardımcı: akış ekleme
    # -----------------------------------------------------------------------

    def _add_flow(self, dp, priority, match, actions,
                  idle_timeout=0, hard_timeout=0, meter_id=None):
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser

        inst_actions = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions)]

        instructions = inst_actions
        if meter_id:
            meter_inst = [parser.OFPInstructionMeter(meter_id)]
            instructions = meter_inst + inst_actions

        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        dp.send_msg(mod)

    def _delete_all_flows(self, dp):
        """Tüm akış kurallarını siler (table-miss hariç)."""
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser
        match   = parser.OFPMatch()
        mod     = parser.OFPFlowMod(
            datapath=dp,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            priority=1,
            match=match,
        )
        dp.send_msg(mod)

    # -----------------------------------------------------------------------
    # Meter (hız sınırı) kurma
    # -----------------------------------------------------------------------

    def _add_meter(self, dp, meter_id, rate_kbps):
        """HTB meter ekler (kbps cinsinden)."""
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser

        bands = [parser.OFPMeterBandDrop(
            type_=ofproto.OFPMBT_DROP,
            rate=rate_kbps,
            burst_size=rate_kbps // 10,
        )]
        mod = parser.OFPMeterMod(
            datapath=dp,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=meter_id,
            bands=bands,
        )
        dp.send_msg(mod)

    def _modify_meter(self, dp, meter_id, rate_kbps):
        """Mevcut meter'ı günceller."""
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser

        bands = [parser.OFPMeterBandDrop(
            type_=ofproto.OFPMBT_DROP,
            rate=max(100, rate_kbps),
            burst_size=max(10, rate_kbps // 10),
        )]
        mod = parser.OFPMeterMod(
            datapath=dp,
            command=ofproto.OFPMC_MODIFY,
            flags=ofproto.OFPMF_KBPS,
            meter_id=meter_id,
            bands=bands,
        )
        dp.send_msg(mod)

    def _delete_meter(self, dp, meter_id):
        ofproto = dp.ofproto
        parser  = dp.ofproto_parser
        mod     = parser.OFPMeterMod(
            datapath=dp,
            command=ofproto.OFPMC_DELETE,
            meter_id=meter_id,
        )
        dp.send_msg(mod)

    # -----------------------------------------------------------------------
    # Mod Uygulama
    # -----------------------------------------------------------------------

    def _apply_mode(self, dp):
        """Mevcut moda göre flow tablosunu ve meter'ları günceller."""
        with self.lock:
            LOG.info('Mod uygulanıyor: %s (dpid=%s)', self.mode, dp.id)

            # Önce temizle
            self._delete_all_flows(dp)
            hub.sleep(0.1)

            # Table-miss yeniden ekle
            ofproto = dp.ofproto
            parser  = dp.ofproto_parser
            match   = parser.OFPMatch()
            action  = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                              ofproto.OFPCML_NO_BUFFER)]
            self._add_flow(dp, 0, match, action)

            if self.mode == 'baseline':
                self._setup_baseline(dp)
            elif self.mode == 'static':
                self._setup_static(dp)
            elif self.mode == 'adaptive':
                self._setup_adaptive_initial(dp)

    def _setup_baseline(self, dp):
        """Baseline: tüm trafik eşit öncelik."""
        LOG.info('[Baseline] QoS kuralı yok.')

    def _setup_static(self, dp):
        """
        Statik QoS:
          - UDP 5001 (VoIP)  → yüksek öncelik
          - UDP 5002 (Video) → orta öncelik
          - TCP               → düşük öncelik + meter (kısıt)
        """
        parser  = dp.ofproto_parser
        ofproto = dp.ofproto

        # Meter ekle (Bulk TCP için)
        try:
            self._delete_meter(dp, METER_BULK_ID)
        except Exception:
            pass
        self._add_meter(dp, METER_BULK_ID, self.bulk_limit_kbps)

        # VoIP (UDP dst 5001) → yüksek öncelik, düz ilet
        match_voip = parser.OFPMatch(eth_type=0x0800,
                                     ip_proto=17,
                                     udp_dst=VOIP_DST_PORT)
        self._add_flow(dp, PRIO_HIGH, match_voip,
                       [parser.OFPActionOutput(ofproto.OFPP_NORMAL)])

        # Video (UDP dst 5002) → orta öncelik
        match_video = parser.OFPMatch(eth_type=0x0800,
                                      ip_proto=17,
                                      udp_dst=VIDEO_DST_PORT)
        self._add_flow(dp, PRIO_MEDIUM, match_video,
                       [parser.OFPActionOutput(ofproto.OFPP_NORMAL)])

        # Bulk TCP → meter üzerinden (kısıtlı)
        match_tcp = parser.OFPMatch(eth_type=0x0800, ip_proto=6)
        self._add_flow(dp, PRIO_LOW, match_tcp,
                       [parser.OFPActionOutput(ofproto.OFPP_NORMAL)],
                       meter_id=METER_BULK_ID)

        LOG.info('[StatikQoS] Bulk kısıt=%d kbps', self.bulk_limit_kbps)

    def _setup_adaptive_initial(self, dp):
        """Adaptif mod başlangıcı: kısıtsız, VoIP izleniyor."""
        parser  = dp.ofproto_parser
        ofproto = dp.ofproto

        # Başlangıçta meter yok (kısıt yok)
        match_voip = parser.OFPMatch(eth_type=0x0800,
                                     ip_proto=17,
                                     udp_dst=VOIP_DST_PORT)
        self._add_flow(dp, PRIO_HIGH, match_voip,
                       [parser.OFPActionOutput(ofproto.OFPP_NORMAL)])

        match_video = parser.OFPMatch(eth_type=0x0800,
                                      ip_proto=17,
                                      udp_dst=VIDEO_DST_PORT)
        self._add_flow(dp, PRIO_MEDIUM, match_video,
                       [parser.OFPActionOutput(ofproto.OFPP_NORMAL)])

        LOG.info('[AdaptifQoS] Başlatıldı, kısıt yok.')

    # -----------------------------------------------------------------------
    # İzleme & Adaptif Karar Döngüsü
    # -----------------------------------------------------------------------

    def _monitor_loop(self):
        """Periyodik istatistik toplar; adaptif modda karar alır."""
        while True:
            hub.sleep(MONITOR_INTV)
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)

    def _request_stats(self, dp):
        parser = dp.ofproto_parser
        req    = parser.OFPFlowStatsRequest(dp)
        dp.send_msg(req)

    # -----------------------------------------------------------------------
    # Dışarıdan Mod Değiştirme (REST API'den çağrılır)
    # -----------------------------------------------------------------------

    def set_mode(self, mode, bulk_limit_mbps=2):
        self.mode             = mode
        self.bulk_limit_kbps  = int(bulk_limit_mbps * 1000)
        self.constrained      = False
        for dp in list(self.datapaths.values()):
            self._apply_mode(dp)
        LOG.info('Mod değiştirildi: %s', mode)

    def update_bulk_limit(self, rate_kbps):
        """Adaptif modda bulk meter'ı günceller."""
        self.bulk_limit_kbps = rate_kbps
        for dp in list(self.datapaths.values()):
            try:
                self._modify_meter(dp, METER_BULK_ID, rate_kbps)
            except Exception:
                try:
                    self._add_meter(dp, METER_BULK_ID, rate_kbps)
                    parser  = dp.ofproto_parser
                    ofproto = dp.ofproto
                    match_tcp = parser.OFPMatch(eth_type=0x0800, ip_proto=6)
                    self._add_flow(dp, PRIO_LOW, match_tcp,
                                   [parser.OFPActionOutput(ofproto.OFPP_NORMAL)],
                                   meter_id=METER_BULK_ID)
                except Exception as e:
                    LOG.error('Meter güncellenemedi: %s', e)
