import { useEffect, useState } from 'react';
import {
  CalendarDays, Clock3, Database, FolderOpen, Layers3, Radio, RefreshCw, Search, Server,
  ShieldCheck, Timer,
} from 'lucide-react';
import { api } from '../lib/api';
import { cleanSymbolInput, dateTime, displaySymbol, integer, shortDate } from '../lib/format';
import { Badge, EmptyState, ErrorBanner, Field, Loading, PageHeader, Panel, SuccessBanner, SyntheticNotice } from '../components/Ui';
import type { Dataset, MarketSourceStatus } from '../types';

const timeframeLabels: Record<string, string> = { daily: '日线', tick: '快照', '1m': '1 分钟', '5m': '5 分钟' };

const defaultStart = () => {
  const date = new Date();
  date.setFullYear(date.getFullYear() - 1);
  return date.toISOString().slice(0, 10);
};
const today = () => new Date().toISOString().slice(0, 10);

export default function DataPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [sources, setSources] = useState<MarketSourceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [symbol, setSymbol] = useState('688200');
  const [startDate, setStartDate] = useState(defaultStart());
  const [endDate, setEndDate] = useState(today());
  const [includeIntraday, setIncludeIntraday] = useState(true);
  const [fetching, setFetching] = useState(false);

  const load = async () => {
    setLoading(true); setError('');
    try {
      const [datasetResult, sourceResult] = await Promise.all([api.datasets(), api.marketSources()]);
      setDatasets(datasetResult);
      setSources(sourceResult);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '行情源与数据目录读取失败。');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const fetchMarket = async () => {
    const code = cleanSymbolInput(symbol);
    if (code.length !== 6) { setError('请输入 6 位 A 股代码，例如 688200 / 300855；沪深交易所由后端自动识别。'); return; }
    if (startDate && endDate && startDate > endDate) { setError('开始日期不能晚于结束日期。'); return; }
    setFetching(true); setError(''); setSuccess('');
    try {
      const result = await api.fetchMarketData({
        symbol: code, start_date: startDate || undefined,
        end_date: endDate || undefined, include_intraday: includeIntraday,
      });
      await load();
      const minuteRows = result.intraday ? `，5分钟 ${integer(result.intraday.rows)} 行` : '';
      const resolved = [result.code || displaySymbol(result.symbol), result.board].filter(Boolean).join(' · ');
      setSuccess(`已接入 ${result.name || resolved}（${resolved}）：日线 ${integer(result.daily.rows)} 行${minuteRows}，对应指数 ${result.index_symbol || ''}。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '行情接入失败，请检查代码、日期区间与网络。');
    } finally {
      setFetching(false);
    }
  };

  const totalRows = datasets.reduce((sum, item) => sum + (Number(item.rows) || 0), 0);
  const sourceCount = new Set(datasets.map((item) => item.source)).size;
  const minuteDatasets = datasets.filter((item) => item.timeframe === '5m').length;
  const intervalMinutes = Math.max(1, Math.round((sources?.realtime_interval_seconds ?? 300) / 60));

  return (
    <div className="page data-page">
      <PageHeader
        eyebrow="MARKET / SOURCES"
        title="行情源中心"
        description="历史日线来自 quan_parquet / baostock，实时快照与 5 分钟 K 线直接接入腾讯行情；本地缓存后供回测与模拟盘复用。"
        actions={<button className="button secondary" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''} />刷新</button>}
      />
      <div className="source-disclosure"><Radio size={16} /><div><strong>只读行情，不连接券商</strong><span>系统仅向 baostock / 腾讯发起行情请求；本地撮合账户不会发送任何真实委托。</span></div></div>
      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}
      {success && <SuccessBanner onClose={() => setSuccess('')}>{success}</SuccessBanner>}

      <div className="data-stats-grid">
        <div><span className="stat-icon indigo"><Database size={19} /></span><div><small>已缓存数据集</small><strong>{integer(datasets.length)}</strong><span>来自真实行情源</span></div></div>
        <div><span className="stat-icon"><Layers3 size={19} /></span><div><small>总数据行数</small><strong>{integer(totalRows)}</strong><span>日线 + 5 分钟</span></div></div>
        <div><span className="stat-icon amber"><Server size={19} /></span><div><small>数据来源</small><strong>{integer(sourceCount)}</strong><span>{Array.from(new Set(datasets.map((item) => item.source))).join(' · ') || 'baostock · tencent'}</span></div></div>
        <div><span className="stat-icon green"><Clock3 size={19} /></span><div><small>5分钟数据集</small><strong>{integer(minuteDatasets)}</strong><span>实时刷新间隔 {intervalMinutes} 分钟</span></div></div>
      </div>

      <div className="data-tools-grid">
        <Panel className="source-fetch-panel" title="接入行情数据" description="输入标的与区间，后端直接拉取并缓存" action={<Search size={19} />}>
          <div className="form-grid source-fetch-form">
            <Field label="标的代码" hint="6 位数字，自动识别沪深"><input value={symbol} onChange={(event) => setSymbol(cleanSymbolInput(event.target.value))} placeholder="例如 688200" inputMode="numeric" maxLength={6} /></Field>
            <Field label="标的名称" hint="自动识别"><input value="由行情源返回" disabled /></Field>
            <Field label="开始日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={startDate} max={endDate} onChange={(event) => setStartDate(event.target.value)} /></div></Field>
            <Field label="结束日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={endDate} min={startDate} onChange={(event) => setEndDate(event.target.value)} /></div></Field>
            <label className="switch-row source-switch"><span><strong>同时接入腾讯 5 分钟</strong><small>用于盘中路径与模拟盘</small></span><input type="checkbox" checked={includeIntraday} onChange={(event) => setIncludeIntraday(event.target.checked)} /><span className="switch" /></label>
          </div>
          <button className="button primary source-fetch-button" onClick={() => void fetchMarket()} disabled={fetching}>
            {fetching ? <><span className="button-spinner" />正在拉取行情</> : <><Search size={16} />接入 历史日线 + 腾讯</>}
          </button>
          <div className="source-note"><ShieldCheck size={15} /><span>日线使用 quan_parquet / baostock 前复权口径；5 分钟与实时快照来自腾讯。缓存文件保存在后端 data/cache 目录。</span></div>
        </Panel>

        <Panel className="source-matrix-panel" title="数据源职责" description="两个来源的分工、粒度与刷新频率" action={<Server size={19} />}>
          <div className="source-matrix">
            <div className="source-card">
              <div className="source-head"><span className="source-icon"><Database size={18} /></span><div><strong>baostock</strong><small>历史日线</small></div><Badge tone="green">已安装</Badge></div>
              <p>提供前复权 OHLCV、成交额、换手率与昨收，用于 16 笔序列、A3、大盘门与完整轮次回测。</p>
              <span>输出粒度：daily</span>
            </div>
            <div className="source-card">
              <div className="source-head"><span className="source-icon indigo"><Radio size={18} /></span><div><strong>腾讯行情</strong><small>实时快照 / 5 分钟</small></div><Badge tone="indigo">已接入</Badge></div>
              <p>提供最新价、盘口一档、成交与换手，用于模拟盘撮合、止盈状态扫描与做 T 准入。</p>
              <span>刷新间隔：{intervalMinutes} 分钟</span>
            </div>
          </div>
          <div className="source-precision"><Timer size={15} /><p>准实时扫描使用 5 分钟 K 线；信号确认使用连续 3 根 5 分钟 K 线，不再使用 1 分钟或 10 秒 tick。</p></div>
          {sources?.note && <div className="source-note plain"><Info size={15} /><span>{String(sources.note)}</span></div>}
        </Panel>
      </div>

      <Panel className="dataset-panel" title="缓存数据集" description="来源、覆盖区间与粒度均来自后端元数据" action={<Badge>{datasets.length} 个数据集</Badge>}>
        {loading ? <Loading label="正在读取行情缓存" /> : datasets.length === 0 ? <EmptyState icon={<FolderOpen size={25} />} title="暂无行情缓存" description="输入标的代码并点击接入，后端会从历史数据源与腾讯拉取数据并落盘。" /> : (
          <div className="table-scroll"><table className="dataset-table"><thead><tr><th>数据集</th><th>标的</th><th>粒度</th><th>数据来源</th><th className="num">行数</th><th>覆盖区间</th><th>创建时间</th><th>属性</th></tr></thead><tbody>
            {datasets.map((dataset) => <tr key={dataset.id}><td><div className="dataset-name"><span><Database size={17} /></span><div><strong>{dataset.name}</strong><small>ID {dataset.id.slice(0, 12)}</small></div></div></td><td><strong title={dataset.symbol}>{displaySymbol(dataset.symbol)}</strong></td><td><Badge tone="indigo">{timeframeLabels[dataset.timeframe] || dataset.timeframe}</Badge><small className="cell-code">{dataset.timeframe}</small></td><td>{dataset.source}</td><td className="num">{integer(dataset.rows)}</td><td><div className="date-range"><span>{shortDate(dataset.start)}</span><i>→</i><span>{shortDate(dataset.end)}</span></div></td><td>{dateTime(dataset.created_at)}</td><td>{dataset.is_synthetic ? <SyntheticNotice compact /> : <Badge tone="green">真实源</Badge>}</td></tr>)}
          </tbody></table></div>
        )}
      </Panel>
    </div>
  );
}

function Info({ size }: { size: number }) {
  return <span style={{ display: 'inline-flex' }}><svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" /></svg></span>;
}
