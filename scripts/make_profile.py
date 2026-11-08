"""仅远窗域名使用本地加密 DNS，iOS 26+ 可故障回退，不配置全局 DNS。"""
import plistlib,uuid
from pathlib import Path
def build_profile():
    payload={'PayloadType':'com.apple.dnsSettings.managed','PayloadVersion':1,'PayloadIdentifier':'cn.ericlab.remote-window.lan-dns','PayloadUUID':str(uuid.uuid4()),'PayloadDisplayName':'远窗同网加速（可自动回退）','DNSSettings':{'DNSProtocol':'HTTPS','ServerURL':'https://xiaoe.ericlab.cn:8768/dns-query','ServerAddresses':['192.168.31.208'],'SupplementalMatchDomains':['xiaoe.ericlab.cn'],'AllowFailover':True},'OnDemandRules':[{'Action':'Connect','InterfaceTypeMatch':'WiFi'},{'Action':'Disconnect'}]}
    profile={'PayloadType':'Configuration','PayloadVersion':1,'PayloadIdentifier':'cn.ericlab.remote-window.lan-dns-profile','PayloadUUID':str(uuid.uuid4()),'PayloadDisplayName':'远窗同网加速 · iOS/iPadOS 26+','PayloadDescription':'仅远窗域名通过 Mac 加密 DNS 解析；Mac 不可达时回退系统 DNS。其它网站不受影响。无需添加根证书、无需开启 VPN。需要 iOS 26、iPadOS 26 或更新系统。','PayloadContent':[payload]}
    return profile

if __name__=='__main__':
    path=Path(__file__).with_name('remote-window-auto.mobileconfig')
    path.write_bytes(plistlib.dumps(build_profile()))
    print('手机描述文件已生成（仅远窗域名、允许自动回退）')
