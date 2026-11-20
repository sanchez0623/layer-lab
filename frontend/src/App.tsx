import { lazy, Suspense, useEffect, useState } from 'react';
import Layout from './components/Layout';
import { Loading } from './components/Ui';
import { Toaster } from '@/components/ui/sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import { AuthProvider, useAuth } from './context/AuthContext';
import LoginPage from './pages/LoginPage';
import type { PageKey } from './types';

const BacktestPage = lazy(() => import('./pages/BacktestPage'));
const DataPage = lazy(() => import('./pages/DataPage'));
const PaperPage = lazy(() => import('./pages/PaperPage'));
const ScreenerPage = lazy(() => import('./pages/ScreenerPage'));
const StrategyPage = lazy(() => import('./pages/StrategyPage'));

const validPages: PageKey[] = ['backtest', 'paper', 'data', 'strategy', 'screener'];

/** hash 路由解析：#/backtest = 列表；#/backtest/<reportId> = 该报告详情；其余页面键不带子资源。 */
const parseHash = (): { page: PageKey; reportId?: string } => {
  const segments = window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const key = (segments[0] ?? '') as PageKey;
  const page = validPages.includes(key) ? key : 'backtest';
  // 仅 backtest 页支持子资源段（报告 id），其它页面忽略多余段以不破坏既有解析。
  const reportId = page === 'backtest' && segments[1] ? decodeURIComponent(segments[1]) : undefined;
  return { page, reportId };
};

export default function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}

/** 登录门禁：initializing 显示 Loading；未登录只渲染登录页；已登录才渲染工作台。
 *  路由解析（含 t2 的 backtest reportId 子资源）保持不变。 */
function AuthGate() {
  const { user, initializing } = useAuth();
  const [{ page, reportId }, setRoute] = useState(parseHash);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const navigate = (next: PageKey) => {
    window.location.hash = `/${next}`;
    setRoute({ page: next });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  if (initializing) {
    return (
      <div className="grid min-h-screen place-items-center bg-background">
        <Loading label="正在校验登录状态" />
      </div>
    );
  }

  if (!user) {
    return (
      <TooltipProvider>
        <LoginPage />
        <Toaster position="top-right" richColors closeButton />
      </TooltipProvider>
    );
  }

  return (
    <TooltipProvider>
      <Layout page={page} onNavigate={navigate}>
        <Suspense fallback={<Loading label="正在载入工作空间" />}>
          {page === 'screener' && <ScreenerPage />}
          {page === 'backtest' && <BacktestPage initialReportId={reportId} />}
          {page === 'paper' && <PaperPage />}
          {page === 'data' && <DataPage />}
          {page === 'strategy' && <StrategyPage />}
        </Suspense>
      </Layout>
      <Toaster position="top-right" richColors closeButton />
    </TooltipProvider>
  );
}
