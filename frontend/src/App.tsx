import { lazy, Suspense, useEffect, useState } from 'react';
import Layout from './components/Layout';
import { Loading } from './components/Ui';
import type { PageKey } from './types';

const BacktestPage = lazy(() => import('./pages/BacktestPage'));
const DataPage = lazy(() => import('./pages/DataPage'));
const PaperPage = lazy(() => import('./pages/PaperPage'));
const ScreenerPage = lazy(() => import('./pages/ScreenerPage'));
const StrategyPage = lazy(() => import('./pages/StrategyPage'));

const validPages: PageKey[] = ['backtest', 'paper', 'data', 'strategy', 'screener'];
const getPage = (): PageKey => {
  const key = window.location.hash.replace('#/', '') as PageKey;
  return validPages.includes(key) ? key : 'backtest';
};

export default function App() {
  const [page, setPage] = useState<PageKey>(getPage);

  useEffect(() => {
    const onHashChange = () => setPage(getPage());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const navigate = (next: PageKey) => {
    window.location.hash = `/${next}`;
    setPage(next);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <Layout page={page} onNavigate={navigate}>
      <Suspense fallback={<Loading label="正在载入工作空间" />}>
        {page === 'screener' && <ScreenerPage />}
        {page === 'backtest' && <BacktestPage />}
        {page === 'paper' && <PaperPage />}
        {page === 'data' && <DataPage />}
        {page === 'strategy' && <StrategyPage />}
      </Suspense>
    </Layout>
  );
}
