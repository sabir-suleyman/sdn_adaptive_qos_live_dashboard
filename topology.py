#!/usr/bin/env python3
"""
SDN Tabanlı Adaptif QoS Demo - Mininet Topolojisi
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI
import argparse
import threading
import time
import json
import subprocess
import socket
import os
import signal

# Dashboard'a metrik göndermek için WebSocket sunucusu portu
METRICS_PORT = 9999

# Topoloji parametreleri
LINK_BW    = 10   # Mbps - ana bağlantı bant genişliği
LINK_DELAY = '5ms'
LINK_LOSS  = 0    # %

class MetricsBroadcaster:
    """Toplanan metrikleri UDP ile dashboard'a iletir."""

    def __init__(self, port=METRICS_PORT):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, data: dict):
        try:
            payload = json.dumps(data).encode()
            self.sock.sendto(payload, ('127.0.0.1', self.port))
        except Exception as e:
            pass  # Dashboard bağlı değilse sessizce geç


broadcaster = MetricsBroadcaster()


def measure_iperf(src, dst_ip, duration=3, udp=False, bandwidth='2M'):
    """
    iperf3 ile throughput, jitter ve paket kaybı ölçer.
    """
    proto_flag = '-u' if udp else ''
    bw_flag    = f'-b {bandwidth}' if udp else ''
    cmd = (f'iperf3 -c {dst_ip} -t {duration} {proto_flag} {bw_flag} '
           f'--json 2>/dev/null')
    result = src.cmd(cmd)
    try:
        data = json.loads(result)
        end  = data['end']
        if udp:
            s   = end['sum']
            return {
                'throughput_mbps': round(s['bits_per_second'] / 1e6, 2),
                'jitter_ms'      : round(s.get('jitter_ms', 0), 2),
                'loss_pct'       : round(s.get('lost_percent', 0), 2),
                'retransmits'    : 0,
            }
        else:
            s   = end['sum_sent']
            return {
                'throughput_mbps': round(s['bits_per_second'] / 1e6, 2),
                'jitter_ms'      : 0.0,
                'loss_pct'       : 0.0,
                'retransmits'    : s.get('retransmits', 0),
            }
    except Exception:
        return {'throughput_mbps': 0, 'jitter_ms': 0, 'loss_pct': 100, 'retransmits': 0}


def measure_ping(src, dst_ip, count=5):
    """RTT ölçer, döndürür: float ms"""
    out = src.cmd(f'ping -c {count} -q {dst_ip} 2>/dev/null')
    try:
        # örnek: rtt min/avg/max/mdev = 4.123/5.456/6.789/0.500 ms
        line = [l for l in out.split('\n') if 'rtt' in l][0]
        avg  = float(line.split('/')[4])
        return round(avg, 2)
    except Exception:
        return -1.0


def apply_tc_limit(host, iface, rate_mbit):
    """
    Linux tc ile host arayüzüne bant genişliği kısıtlaması uygular.
    rate_mbit=0 ise kısıtlamayı kaldırır.
    """
    host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null')
    if rate_mbit > 0:
        host.cmd(
            f'tc qdisc add dev {iface} root tbf '
            f'rate {rate_mbit}mbit burst 32kbit latency 400ms'
        )


def run_iperf_server(host):
    """Arka planda iperf3 sunucusu başlatır."""
    host.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    host.cmd('iperf3 -s -D')   # daemon modunda


def build_network():
    """
    Topoloji:
        h1 (VoIP/UDP) ──┐
        h2 (Video/UDP) ──┤── s1 ── s2 ── h4 (sunucu)
        h3 (Bulk/TCP) ──┘
    """
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )

    info('*** Kontrolcü ekleniyor\n')
    c0 = net.addController('c0', ip='127.0.0.1', port=6633)

    info('*** Switch\'ler ekleniyor\n')
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')

    info('*** Host\'lar ekleniyor\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')   # VoIP (UDP düşük bant)
    h2 = net.addHost('h2', ip='10.0.0.2/24')   # Video (UDP orta bant)
    h3 = net.addHost('h3', ip='10.0.0.3/24')   # Bulk  (TCP yüksek bant)
    h4 = net.addHost('h4', ip='10.0.0.4/24')   # Sunucu

    info('*** Linkler ekleniyor\n')
    link_opts = dict(bw=LINK_BW, delay=LINK_DELAY, loss=LINK_LOSS, use_htb=True)
    net.addLink(h1, s1, **link_opts)
    net.addLink(h2, s1, **link_opts)
    net.addLink(h3, s1, **link_opts)
    net.addLink(s1, s2, bw=LINK_BW, delay=LINK_DELAY, loss=LINK_LOSS, use_htb=True)
    net.addLink(s2, h4, **link_opts)

    return net, h1, h2, h3, h4


# SENARYO FONKSİYONLARI

def run_baseline(net, h1, h2, h3, h4, stop_event):
    """
    Senaryo 1 – Baseline: QoS kuralı yok, tüm trafik eşit.
    """
    info('\n=== BASELINE SENARYOSU BAŞLADI ===\n')
    run_iperf_server(h4)
    time.sleep(1)

    while not stop_event.is_set():
        t0 = time.time()

        voip  = measure_iperf(h1, '10.0.0.4', duration=2, udp=True,  bandwidth='0.5M')
        video = measure_iperf(h2, '10.0.0.4', duration=2, udp=True,  bandwidth='3M')
        bulk  = measure_iperf(h3, '10.0.0.4', duration=2, udp=False)
        rtt   = measure_ping(h1, '10.0.0.4', count=3)

        payload = {
            'scenario'  : 'baseline',
            'timestamp' : round(time.time(), 2),
            'voip'      : voip,
            'video'     : video,
            'bulk'      : bulk,
            'rtt_ms'    : rtt,
        }
        broadcaster.send(payload)
        info(f'[Baseline] VoIP={voip["throughput_mbps"]}Mbps '
             f'Video={video["throughput_mbps"]}Mbps '
             f'Bulk={bulk["throughput_mbps"]}Mbps RTT={rtt}ms\n')

        elapsed = time.time() - t0
        stop_event.wait(max(0, 6 - elapsed))


def run_static_qos(net, h1, h2, h3, h4, stop_event,
                   bulk_limit_mbps=2):
    """
    Senaryo 2 – Statik QoS: Bulk TCP trafiğine sabit bant genişliği kısıtı uygulanır.
    """
    info(f'\n=== STATİK QoS SENARYOSU BAŞLADI '
         f'(Bulk kısıt={bulk_limit_mbps} Mbps) ===\n')
    run_iperf_server(h4)
    time.sleep(0.5)

    # h3'ün s1'e bakan arayüzü (Mininet otomatik adlandırır: h3-eth0)
    apply_tc_limit(h3, 'h3-eth0', bulk_limit_mbps)

    while not stop_event.is_set():
        t0 = time.time()

        voip  = measure_iperf(h1, '10.0.0.4', duration=2, udp=True,  bandwidth='0.5M')
        video = measure_iperf(h2, '10.0.0.4', duration=2, udp=True,  bandwidth='3M')
        bulk  = measure_iperf(h3, '10.0.0.4', duration=2, udp=False)
        rtt   = measure_ping(h1, '10.0.0.4', count=3)

        payload = {
            'scenario'        : 'static',
            'timestamp'       : round(time.time(), 2),
            'voip'            : voip,
            'video'           : video,
            'bulk'            : bulk,
            'rtt_ms'          : rtt,
            'bulk_limit_mbps' : bulk_limit_mbps,
        }
        broadcaster.send(payload)
        info(f'[StatikQoS] VoIP={voip["throughput_mbps"]}Mbps '
             f'Video={video["throughput_mbps"]}Mbps '
             f'Bulk={bulk["throughput_mbps"]}Mbps (limit={bulk_limit_mbps}M) '
             f'RTT={rtt}ms\n')

        elapsed = time.time() - t0
        stop_event.wait(max(0, 6 - elapsed))

    # Temizlik: kısıtı kaldır
    apply_tc_limit(h3, 'h3-eth0', 0)


def run_adaptive_qos(net, h1, h2, h3, h4, stop_event):
    """
    Senaryo 3 – Adaptif QoS:
    Ağ durumunu izler; Bulk trafik UDP'yi bastırıyorsa otomatik kısıtlar.
    """
    VOIP_LOSS_THRESH = 2.0    # % paket kaybı eşiği
    VOIP_MIN_MBPS   = 0.3    # minimum kabul edilebilir VoIP throughput
    BULK_MIN_LIMIT  = 1.0    # en düşük bulk kısıt (Mbps)
    BULK_MAX_LIMIT  = LINK_BW - 1  # kısıtsız üst sınır

    current_limit = BULK_MAX_LIMIT   # başlangıçta kısıt yok
    constrained   = False

    info('\n=== ADAPTİF QoS SENARYOSU BAŞLADI ===\n')
    run_iperf_server(h4)
    time.sleep(0.5)

    while not stop_event.is_set():
        t0 = time.time()

        voip  = measure_iperf(h1, '10.0.0.4', duration=2, udp=True,  bandwidth='0.5M')
        video = measure_iperf(h2, '10.0.0.4', duration=2, udp=True,  bandwidth='3M')
        bulk  = measure_iperf(h3, '10.0.0.4', duration=2, udp=False)
        rtt   = measure_ping(h1, '10.0.0.4', count=3)

        # --- Karar Mekanizması ---
        voip_unhealthy = (
            voip['loss_pct']       > VOIP_LOSS_THRESH or
            voip['throughput_mbps'] < VOIP_MIN_MBPS
        )

        action = 'none'
        if voip_unhealthy and current_limit > BULK_MIN_LIMIT:
            # Bulk trafiği kısıtla (yarıya indir, ama minimumun altına düşme)
            new_limit = max(BULK_MIN_LIMIT, current_limit * 0.6)
            if abs(new_limit - current_limit) > 0.1:
                current_limit = new_limit
                apply_tc_limit(h3, 'h3-eth0', current_limit)
                constrained = True
                action = f'KISITLANDI → {current_limit:.1f} Mbps'
                info(f'[Adaptif] ⚠ VoIP sağlıksız! Bulk kısıtlandı: {current_limit:.1f} Mbps\n')

        elif not voip_unhealthy and constrained:
            # Yavaşça gevşet
            new_limit = min(BULK_MAX_LIMIT, current_limit * 1.3)
            if abs(new_limit - current_limit) > 0.1:
                current_limit = new_limit
                apply_tc_limit(h3, 'h3-eth0', current_limit)
                action = f'GEVŞETİLDİ → {current_limit:.1f} Mbps'
                info(f'[Adaptif] ✓ VoIP sağlıklı, Bulk gevşetiliyor: {current_limit:.1f} Mbps\n')
            if current_limit >= BULK_MAX_LIMIT:
                constrained = False
                apply_tc_limit(h3, 'h3-eth0', 0)
                action = 'SERBEST'

        payload = {
            'scenario'     : 'adaptive',
            'timestamp'    : round(time.time(), 2),
            'voip'         : voip,
            'video'        : video,
            'bulk'         : bulk,
            'rtt_ms'       : rtt,
            'bulk_limit'   : round(current_limit, 1),
            'constrained'  : constrained,
            'action'       : action,
            'voip_healthy' : not voip_unhealthy,
        }
        broadcaster.send(payload)
        info(f'[Adaptif] VoIP={voip["throughput_mbps"]}Mbps({voip["loss_pct"]}% kayıp) '
             f'Video={video["throughput_mbps"]}Mbps '
             f'Bulk={bulk["throughput_mbps"]}Mbps '
             f'BulkLimit={current_limit:.1f}Mbps RTT={rtt}ms\n')

        elapsed = time.time() - t0
        stop_event.wait(max(0, 6 - elapsed))

    apply_tc_limit(h3, 'h3-eth0', 0)


# Main fonksiyonu


def main():
    parser = argparse.ArgumentParser(description='SDN Adaptif QoS Demo')
    parser.add_argument('--mode', choices=['baseline', 'static', 'adaptive', 'cli'],
                        default='cli',
                        help='Çalışma modu (varsayılan: cli)')
    parser.add_argument('--bulk-limit', type=float, default=2.0,
                        help='Statik mod için Bulk TCP kısıt (Mbps)')
    args = parser.parse_args()

    setLogLevel('info')
    net, h1, h2, h3, h4 = build_network()

    info('*** Ağ başlatılıyor\n')
    net.start()
    net.waitConnected()
    time.sleep(2)   # controller'ın flow table'ı öğrenmesi için

    stop_event = threading.Event()

    mode_fn = {
        'baseline': lambda: run_baseline(net, h1, h2, h3, h4, stop_event),
        'static'  : lambda: run_static_qos(net, h1, h2, h3, h4, stop_event,
                                           args.bulk_limit),
        'adaptive': lambda: run_adaptive_qos(net, h1, h2, h3, h4, stop_event),
    }

    if args.mode == 'cli':
        info('*** CLI modu: manuel komutlar için Mininet CLI açılıyor\n')
        CLI(net)
    else:
        thread = threading.Thread(target=mode_fn[args.mode], daemon=True)
        thread.start()

        try:
            info('*** Durdumak için Ctrl+C\n')
            CLI(net)   # CLI açık kalır, arka planda ölçüm devam eder
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            thread.join(timeout=5)

    info('*** Ağ kapatılıyor\n')
    net.stop()


if __name__ == '__main__':
    main()
