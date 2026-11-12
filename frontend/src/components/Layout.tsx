import { useEffect, useState, type ReactNode } from 'react';
import { Activity, BarChart3, BookOpenCheck, ChevronRight, Database, Filter, Layers3, Menu, MonitorDot, Wifi, WifiOff, X } from 'lucide-react';
import { API_BASE, api } from '../lib/api';
import type { PageKey } from '../types';

const navigation: Array<{ key: PageKey; label: string; caption: string; icon: typeof BarChart3 }> = [
  { key: 'screener', label: '选股漏斗', caption: '五层筛选', icon: Filter },
  { key: 'backtest', label: '回测工作台', caption: '策略验证', icon: BarChart3 },
  { key: 'paper', label: '模拟交易', caption: '逐根撮合', icon: Activity },
  { key: 'data', label: '数据中心', caption: '行情管理', icon: Database },
  { key: 'strategy', label: '策略规则', caption: '规则说明书', icon: BookOpenCheck },
];

const titles: Record<PageKey, string> = {
  screener: '选股漏斗', backtest: '回测工作台', paper: '模拟交易', data: '数据中心', strategy: '策略规则',
};

export default function Layout({ page, onNavigate, children }: { page: PageKey; onNavigate: (page: PageKey) => void; children: ReactNode }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    let mounted = true;
    const check = () => api.health().then(() => mounted && setOnline(true)).catch(() => mounted && setOnline(false));
    check();
    const timer = window.setInterval(check, 30000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, []);

  const navigate = (key: PageKey) => { onNavigate(key); setMenuOpen(false); };

  return (
    <div className="app-shell">
      {menuOpen && <button className="nav-scrim" onClick={() => setMenuOpen(false)} aria-label="关闭导航" />}
      <aside className={`sidebar ${menuOpen ? 'is-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark"><Layers3 size={23} strokeWidth={1.8} /></div>
          <div><strong>LayerLab</strong><span>分层交易策略实验室</span></div>
          <button className="icon-button mobile-close" onClick={() => setMenuOpen(false)} aria-label="关闭菜单"><X size={18} /></button>
        </div>
        <div className="nav-section-label">工作空间</div>
        <nav className="main-nav" aria-label="主导航">
          {navigation.map(({ key, label, caption, icon: Icon }) => (
            <button key={key} className={`nav-item ${page === key ? 'active' : ''}`} onClick={() => navigate(key)} aria-current={page === key ? 'page' : undefined}>
              <Icon size={19} strokeWidth={1.8} />
              <span><strong>{label}</strong><small>{caption}</small></span>
              <ChevronRight size={15} className="nav-arrow" />
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="environment-card">
            <div className="environment-title"><MonitorDot size={16} /><strong>本地仿真</strong><span className="status-dot" /></div>
            <p>未连接券商</p>
            <div className="environment-divider" />
            <span>所有交易均为虚拟撮合<br />不产生真实委托或收益</span>
          </div>
          <div className="version-line"><span>LAYERLAB DESKTOP</span><span>v1.0</span></div>
        </div>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-left">
            <button className="icon-button menu-button" onClick={() => setMenuOpen(true)} aria-label="打开菜单"><Menu size={20} /></button>
            <span className="topbar-path">工作空间</span><ChevronRight size={14} /><strong>{titles[page]}</strong>
          </div>
          <div className="topbar-right">
            <span className="local-badge"><span className="status-dot" />本地仿真 / 未连接券商</span>
            <span className={`connection-badge ${online === false ? 'offline' : ''}`} title={`API: ${API_BASE}`}>
              {online === false ? <WifiOff size={14} /> : <Wifi size={14} />}
              {online === null ? '检测后端中' : online ? '后端已连接' : '后端未连接'}
            </span>
          </div>
        </header>
        <main className="content">{children}</main>
        <footer className="app-footer"><span>LayerLab · 策略研究环境</span><span>数据与结果不构成投资建议</span></footer>
      </div>
    </div>
  );
}
