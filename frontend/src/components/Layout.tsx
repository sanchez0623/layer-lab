import { useEffect, useState, type ReactNode } from 'react';
import {
  Activity, BarChart3, BookOpenCheck, ChevronRight, Database, Filter, Layers3, Menu,
  MonitorDot, Wifi, WifiOff,
} from 'lucide-react';
import { API_BASE, api } from '../lib/api';
import type { PageKey } from '../types';
import { Badge } from '../components/Ui';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

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

function SidebarContent({ page, onNavigate }: { page: PageKey; onNavigate: (page: PageKey) => void }) {
  return (
    <>
      <div className="flex h-[70px] items-center gap-2.5 border-b px-2">
        <div className="grid size-9 place-items-center rounded-[10px] bg-primary text-primary-foreground shadow-sm shadow-primary/20">
          <Layers3 size={22} strokeWidth={1.8} />
        </div>
        <div className="min-w-0">
          <strong className="block text-xl font-semibold tracking-[-0.02em] text-foreground">LayerLab</strong>
          <span className="block text-[13px] text-muted-foreground">分层交易策略实验室</span>
        </div>
      </div>

      <div className="px-2 pt-5 text-[11px] font-medium tracking-[0.12em] text-muted-foreground">工作空间</div>
      <nav className="mt-2 flex flex-1 flex-col gap-1" aria-label="主导航">
        {navigation.map(({ key, label, caption, icon: Icon }) => {
          const active = page === key;
          return (
            <button
              key={key}
              type="button"
              className={cn(
                'group flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors',
                active ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
              onClick={() => onNavigate(key)}
              aria-current={active ? 'page' : undefined}
            >
              <span className={cn('grid size-8 shrink-0 place-items-center rounded-md border', active ? 'border-primary/20 bg-card text-primary' : 'border-transparent bg-muted/60')}>
                <Icon size={18} strokeWidth={1.8} />
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <strong className="text-[13px] font-semibold">{label}</strong>
                <small className="mt-0.5 text-[11px] font-normal text-muted-foreground">{caption}</small>
              </span>
              <ChevronRight size={15} className={cn('shrink-0 text-muted-foreground/60 transition-transform', active && 'text-primary')} />
            </button>
          );
        })}
      </nav>

      <div className="mt-auto flex flex-col gap-3 pb-4">
        <div className="rounded-xl border bg-card p-3 shadow-xs">
          <div className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
            <MonitorDot size={16} />
            <strong>本地仿真</strong>
            <span className="ml-auto size-2 rounded-full bg-success shadow-[0_0_0_3px] shadow-success/15" />
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">未连接券商</p>
          <div className="my-2.5 h-px bg-border" />
          <span className="text-[11px] leading-5 text-muted-foreground">所有交易均为虚拟撮合<br />不产生真实委托或收益</span>
        </div>
        <div className="flex items-center justify-between px-1 font-mono text-[10px] tracking-wide text-muted-foreground">
          <span>LAYERLAB DESKTOP</span><span>v2.0</span>
        </div>
      </div>
    </>
  );
}

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
    <div className="min-h-screen bg-background">
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-[252px] flex-col border-r bg-sidebar px-4 lg:flex">
        <SidebarContent page={page} onNavigate={navigate} />
      </aside>

      <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
        <SheetContent side="left" className="w-[280px] max-w-[85vw] px-4">
          <SheetHeader className="sr-only">
            <SheetTitle>主导航</SheetTitle>
            <SheetDescription>切换工作空间页面</SheetDescription>
          </SheetHeader>
          <SidebarContent page={page} onNavigate={navigate} />
        </SheetContent>
      </Sheet>

      <div className="flex min-h-screen flex-col lg:pl-[252px]">
        <header className="sticky top-0 z-30 flex h-[60px] items-center justify-between border-b bg-background/90 px-6 backdrop-blur-md">
          <div className="flex min-w-0 items-center gap-2.5 text-[13px] text-muted-foreground">
            <Button variant="ghost" size="icon-sm" className="lg:hidden" onClick={() => setMenuOpen(true)} aria-label="打开菜单">
              <Menu />
            </Button>
            <span className="hidden md:inline">工作空间</span>
            <ChevronRight size={14} className="hidden md:inline" />
            <strong className="truncate font-medium text-foreground/80">{titles[page]}</strong>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone="neutral" title="本地仿真环境" className="hidden lg:inline-flex">
              <span className="size-1.5 rounded-full bg-success" />
              本地仿真 / 未连接券商
            </Badge>
            <Tooltip>
              <TooltipTrigger
                render={
                  <span className={cn('inline-flex items-center gap-1.5 rounded-md border bg-card px-2.5 py-1.5 text-xs font-medium', online === false ? 'border-destructive/30 text-destructive' : 'text-muted-foreground')} />
                }
              >
                {online === false ? <WifiOff className="size-3.5" /> : <Wifi className="size-3.5" />}
                <span className="hidden sm:inline">{online === null ? '检测后端中' : online ? '后端已连接' : '后端未连接'}</span>
              </TooltipTrigger>
              <TooltipContent>API: {API_BASE}</TooltipContent>
            </Tooltip>
          </div>
        </header>

        <main className="mx-auto w-full max-w-[1640px] flex-1 px-4 py-5 md:px-6">{children}</main>
        <footer className="mx-4 flex flex-wrap items-center justify-between gap-3 border-t py-3 text-xs text-muted-foreground md:mx-6">
          <span>LayerLab · 策略研究环境</span><span>数据与结果不构成投资建议</span>
        </footer>
      </div>
    </div>
  );
}
