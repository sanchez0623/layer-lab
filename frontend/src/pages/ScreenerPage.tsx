import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, CalendarDays, Clock3, Database, Filter, History, Layers3, Play, RefreshCw,
} from 'lucide-react';
import { ApiError, api } from '../lib/api';
import {
  dateTime, displaySymbol, integer, money, number, percent, shortDate, signedPercent,
} from '../lib/format';
import { Badge, EmptyState, ErrorBanner, Field, Loading, PageHeader, Panel, SuccessBanner, TableEmpty } from '../components/Ui';
import type {
  RefreshStatus, ScreenerCandidate, ScreenerConfig, ScreenerCoverage, ScreenerRunSummary, ScreeningRun,
} from '../types';

const BOARD_OPTIONS = ['创业板', '科创板'] as const;

const defaultConfig: ScreenerConfig = {
  boards: ['创业板', '科创板'],
  chg20_max: -0.15,
  week52_drawdown: 0.3,
  day_change_max: 0.05,
  min_amount: 5e8,
  min_avg_amount_20d: 5e8,
  min_listing_sessions: 6,
  exclude_st: true,
  exclude_delisting: true,
  exclude_unprofitable_u: true,
  enable_fundamental_veto: true,
  max_deep_checks: 10,
  blacklist: [],
};

const emptyCoverage: ScreenerCoverage = {
  codes: 0, rows: 0, start: null, end: null, boards: {}, fundamentals: 0,
  updated_at: null, missing_codes: 0, stale_codes: 0,
};

/** 红涨绿跌：>=0 用 positive（红），<0 用 negative（绿）。 */
const toneOf = (value: number) => (value >= 0 ? 'positive' : 'negative');
const percentValue = (value: number) => Number((value * 100).toFixed(2));
const yi = (value?: number | null) => (
  value === undefined || value === null || !Number.isFinite(value) ? '—' : `${number(value / 1e8, 2)} 亿`
);
const fundamentalLabel = (value?: string) => {
  switch (value) {
    case 'applied': return '已应用';
    case 'partial': return '部分降级';
    case 'unavailable': return '不可用';
    case 'disabled': return '未启用';
    default: return '—';
  }
};

export default function ScreenerPage() {
  const [config, setConfig] = useState<ScreenerConfig>(defaultConfig);
  const [includeRealtime, setIncludeRealtime] = useState(false);
  const [coverage, setCoverage] = useState<ScreenerCoverage | null>(null);
  const [refreshStatus, setRefreshStatus] = useState<RefreshStatus | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState<ScreeningRun | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const [history, setHistory] = useState<ScreenerRunSummary[]>([]);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const pollRef = useRef<number | null>(null);
  const pollBusyRef = useRef(false);

  const stopPolling = () => {
    if (pollRef.current !== null) { window.clearInterval(pollRef.current); pollRef.current = null; }
  };

  const pollOnce = async () => {
    if (pollBusyRef.current) return;
    pollBusyRef.current = true;
    try {
      const status = await api.screenerRefreshStatus();
      setRefreshStatus(status);
      if (!status.running) {
        stopPolling();
        setCoverage(status.coverage);
        setSuccess('全市场日线刷新完成，覆盖数据已更新。');
        void api.screenerDefaults()
          .then((defaults) => { setCoverage(defaults.coverage); setNotes(defaults.notes || []); })
          .catch(() => undefined);
      }
    } catch (cause) {
      stopPolling();
      setError(cause instanceof Error ? cause.message : '刷新状态读取失败。');
    } finally {
      pollBusyRef.current = false;
    }
  };

  const startPolling = () => {
    stopPolling();
    pollRef.current = window.setInterval(() => void pollOnce(), 2000);
  };

  const loadRun = async (id: string, silent = false) => {
    if (!silent) setRunLoading(true);
    setError('');
    try {
      setRun(await api.screenerRun(id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '选股结果读取失败。');
    } finally {
      if (!silent) setRunLoading(false);
    }
  };

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [defaultsResult, coverageResult, runs] = await Promise.all([
        api.screenerDefaults(), api.screenerCoverage(), api.screenerRuns(),
      ]);
      setConfig({
        ...defaultConfig,
        ...defaultsResult.config,
        boards: defaultsResult.config.boards.length > 0 ? defaultsResult.config.boards : defaultConfig.boards,
      });
      setCoverage(coverageResult.coverage);
      setRefreshStatus(coverageResult.refresh);
      setNotes(defaultsResult.notes || []);
      setHistory(runs);
      if (coverageResult.refresh.running) startPolling();
      if (runs.length > 0) void loadRun(runs[0].id, true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '选股默认值读取失败，请检查后端服务。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => stopPolling();
    // 仅在挂载时初始化；轮询在 startPolling 内自行清理。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startRefresh = async (kind: 'full' | 'bootstrap') => {
    setRefreshing(true);
    setError('');
    setSuccess('');
    try {
      const result = await api.screenerRefresh({
        boards: config.boards,
        lookback_days: kind === 'bootstrap' ? 60 : 400,
      });
      setRefreshStatus({ running: true, job: result.job, coverage: coverage ?? emptyCoverage });
      startPolling();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setError('刷新任务已在运行，已开始轮询进度。');
        startPolling();
        void pollOnce();
      } else {
        setError(cause instanceof Error ? cause.message : '刷新任务启动失败。');
      }
    } finally {
      setRefreshing(false);
    }
  };

  const runScreen = async () => {
    if (config.boards.length === 0) { setError('请至少选择一个板块（创业板 / 科创板）。'); return; }
    setRunning(true);
    setError('');
    setSuccess('');
    try {
      const result = await api.runScreener({ include_realtime: includeRealtime, config });
      setRun(result);
      setSuccess(`选股完成：池内 ${result.pool.length} 只，可用 ${result.eligible.length} 只，耗时 ${number(result.elapsed_seconds, 2)} 秒。`);
      if (result.data_info.coverage) setCoverage(result.data_info.coverage);
      const runs = await api.screenerRuns().catch(() => null);
      if (runs) setHistory(runs);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '选股运行失败，请检查数据覆盖与后端日志。');
    } finally {
      setRunning(false);
    }
  };

  const bringIn = async (target: 'backtest' | 'paper', candidate: ScreenerCandidate) => {
    if (!run) { setError('请先运行或回看一次选股结果。'); return; }
    setPreparing(`${target}:${candidate.code}`);
    setError('');
    setSuccess('');
    try {
      const prepared = await api.prepareScreenerRun(run.id, {
        target, candidate_code: candidate.code, rotation: target === 'paper',
      });
      if (target === 'backtest') {
        sessionStorage.setItem('layerlab.pendingDatasets', JSON.stringify({
          runId: run.id, candidate: prepared.candidate, datasets: prepared.datasets,
        }));
        window.location.hash = '#/backtest';
      } else {
        sessionStorage.setItem('layerlab.pendingPaper', JSON.stringify({
          runId: run.id, candidate: prepared.candidate, accountId: prepared.account?.id ?? null,
        }));
        window.location.hash = '#/paper';
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '一键带入失败。');
      setPreparing(null);
    }
  };

  const removeRun = async (id: string) => {
    if (!window.confirm('确定删除该条选股运行记录吗？删除后无法恢复。')) return;
    setDeletingId(id);
    setError('');
    try {
      const result = await api.deleteScreenerRun(id);
      if (!result.deleted) setError('后端未找到该运行记录。');
      setHistory((current) => current.filter((item) => item.id !== id));
      if (run?.id === id) setRun(null);
      setSuccess('运行记录已删除。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '运行记录删除失败。');
    } finally {
      setDeletingId(null);
    }
  };

  const patchConfig = (patch: Partial<ScreenerConfig>) => setConfig((current) => ({ ...current, ...patch }));
  const setRatio = (key: 'chg20_max' | 'week52_drawdown' | 'day_change_max', raw: string) => {
    if (raw.trim() === '') return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    setConfig((current) => ({ ...current, [key]: parsed / 100 }));
  };
  const setYi = (key: 'min_amount' | 'min_avg_amount_20d', raw: string) => {
    if (raw.trim() === '') return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    setConfig((current) => ({ ...current, [key]: parsed * 1e8 }));
  };
  const toggleBoard = (board: string) => setConfig((current) => ({
    ...current,
    boards: current.boards.includes(board)
      ? current.boards.filter((item) => item !== board)
      : [...current.boards, board],
  }));

  const job = refreshStatus?.job;
  const refreshRunning = Boolean(refreshStatus?.running);
  const refreshPercent = job && job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
  const boardText = coverage
    ? BOARD_OPTIONS.map((board) => `${board} ${integer(coverage.boards[board] ?? 0)}`).join(' · ')
    : '—';
  const first = run?.first ?? null;
  const funnelStages = run ? [
    { tag: 'UNIVERSE', label: '全市场', value: run.funnel.universe },
    { tag: 'L0', label: '大盘门后', value: run.funnel.after_l0 },
    { tag: 'L1', label: 'A1 超跌', value: run.funnel.after_l1 },
    { tag: 'L2', label: '一票否决后', value: run.funnel.after_l2 },
    { tag: 'L3', label: '容量达标', value: run.funnel.after_l3 },
    { tag: 'L4', label: '可用候选', value: run.funnel.eligible },
  ] : [];

  return (
    <div className="page screener-page">
      <PageHeader
        eyebrow="SCREENER / FUNNEL"
        title="选股漏斗"
        description="L0 大盘同步门 → L1 A1 超跌信号 → L2 一票否决 → L3 容量 → L4 排序取第一；全 A 双创日线落库后逐层收敛候选池。"
        actions={<>
          <button className="button secondary" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={15} className={loading ? 'spin' : ''} />刷新状态
          </button>
          <button className="button primary" onClick={() => void runScreen()} disabled={running || config.boards.length === 0}>
            {running ? <><span className="button-spinner" />正在选股</> : <><Play size={15} fill="currentColor" />运行选股</>}
          </button>
        </>}
      />

      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}
      {success && <SuccessBanner onClose={() => setSuccess('')}>{success}</SuccessBanner>}

      <Panel
        className="screener-coverage-panel"
        title="数据覆盖"
        description="全市场双创日线缓存（baostock 前复权）；回补任务在后台线程运行，可断点续跑"
        action={<Badge tone={coverage && coverage.missing_codes === 0 && coverage.stale_codes === 0 ? 'green' : 'amber'}>
          {coverage ? (coverage.missing_codes === 0 && coverage.stale_codes === 0 ? '覆盖完整' : '需回补') : '未读取'}
        </Badge>}
      >
        {loading && !coverage ? <Loading label="正在读取全市场覆盖情况" /> : !coverage ? (
          <EmptyState
            compact
            icon={<Database size={22} />}
            title="尚未读取覆盖数据"
            description="后端 universe.db 覆盖信息不可用；请确认服务已启动后重试。"
            action={<button className="button secondary" onClick={() => void load()}>重试</button>}
          />
        ) : (
          <>
            <div className="data-stats-grid screener-coverage-stats">
              <div><span className="stat-icon indigo"><Database size={19} /></span><div><small>覆盖代码</small><strong>{integer(coverage.codes)}</strong><span>双创 universe</span></div></div>
              <div><span className="stat-icon"><Layers3 size={19} /></span><div><small>日线行数</small><strong>{integer(coverage.rows)}</strong><span>{shortDate(coverage.start)} → {shortDate(coverage.end)}</span></div></div>
              <div><span className="stat-icon amber"><AlertTriangle size={19} /></span><div><small>未回补票</small><strong>{integer(coverage.missing_codes)}</strong><span>有代码但无日线</span></div></div>
              <div><span className="stat-icon green"><Clock3 size={19} /></span><div><small>过期票</small><strong>{integer(coverage.stale_codes)}</strong><span>最新日线落后全局交易日</span></div></div>
            </div>
            <div className="screener-meta-row">
              <span><CalendarDays size={13} />覆盖区间 {shortDate(coverage.start)} → {shortDate(coverage.end)}</span>
              <span><Layers3 size={13} />板块 {boardText}</span>
              <span><Database size={13} />财务缓存 {integer(coverage.fundamentals)}</span>
              <span><Clock3 size={13} />最后更新 {dateTime(coverage.updated_at)}</span>
            </div>
            {refreshRunning ? (
              <div className="screener-refresh" role="status" aria-live="polite">
                <div className="screener-refresh-head">
                  <div>
                    <span className="live-dot playing" />
                    <strong>正在刷新全市场日线</strong>
                    <small>{job?.message || '后台回补中，可离开本页，稍后回来查看进度'}</small>
                  </div>
                  <Badge tone="indigo">{integer(job?.done)} / {integer(job?.total)} · {refreshPercent}%</Badge>
                </div>
                <div className="screener-progress-track"><i className="screener-progress-fill" style={{ width: `${refreshPercent}%` }} /></div>
                <div className="screener-refresh-meta">
                  <span>当前 {job?.current || '—'}</span>
                  <span>已写入 {integer(job?.fetched_rows)} 行</span>
                  <span>跳过 {integer(job?.skipped)}</span>
                  <span className={job && job.errors.length > 0 ? 'negative' : ''}>错误 {job ? job.errors.length : 0}</span>
                </div>
                {job && job.errors.length > 0 && (
                  <div className="screener-job-errors">
                    {job.errors.slice(-3).map((item, index) => <span key={index}>{item}</span>)}
                  </div>
                )}
              </div>
            ) : (
              <div className="screener-refresh-actions">
                <button className="button primary" onClick={() => void startRefresh('full')} disabled={refreshing}>
                  <RefreshCw size={15} className={refreshing ? 'spin' : ''} />更新全市场日线
                </button>
                <button className="button secondary" onClick={() => void startRefresh('bootstrap')} disabled={refreshing}>
                  <Database size={15} />bootstrap 元数据
                </button>
                <span className="screener-hint">增量更新；首次全量约 1900 票 × 1.2s ≈ 35–50 分钟，后台线程执行。</span>
              </div>
            )}
          </>
        )}
      </Panel>

      <Panel
        className="screener-param-panel"
        title="选股参数"
        description="阈值直接映射后端 ScreenerConfig；界面百分比与亿元会自动换算为小数"
        action={<Filter size={18} />}
      >
        <div className="screener-board-row">
          <span className="screener-board-label">板块</span>
          {BOARD_OPTIONS.map((board) => (
            <button
              key={board}
              className={`screener-board-chip ${config.boards.includes(board) ? 'active' : ''}`}
              onClick={() => toggleBoard(board)}
              aria-pressed={config.boards.includes(board)}
            >
              {board}<small>{board === '创业板' ? 'sz.399006' : 'sh.000688'}</small>
            </button>
          ))}
          <span className="screener-hint">板块决定 L0 使用哪个指数；至少保留一个。</span>
        </div>
        <div className="form-grid screener-param-grid">
          <Field label="A1 20日跌幅阈值" hint="Chg20D < 阈值">
            <div className="input-with-unit"><input type="number" step="1" value={percentValue(config.chg20_max)} onChange={(event) => setRatio('chg20_max', event.target.value)} /><span>%</span></div>
          </Field>
          <Field label="距52周高回撤" hint="Close < High250 × (1 - 回撤)">
            <div className="input-with-unit"><input type="number" min="0" step="1" value={percentValue(config.week52_drawdown)} onChange={(event) => setRatio('week52_drawdown', event.target.value)} /><span>%</span></div>
          </Field>
          <Field label="L3 当日成交额门槛" hint="当日 amount ≥">
            <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((config.min_amount / 1e8).toFixed(2))} onChange={(event) => setYi('min_amount', event.target.value)} /><span>亿</span></div>
          </Field>
          <Field label="L3 20日均额门槛" hint="20 日均成交额 ≥">
            <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((config.min_avg_amount_20d / 1e8).toFixed(2))} onChange={(event) => setYi('min_avg_amount_20d', event.target.value)} /><span>亿</span></div>
          </Field>
          <Field label="当日涨幅上限" hint="L2 超过即否决">
            <div className="input-with-unit"><input type="number" step="1" value={percentValue(config.day_change_max)} onChange={(event) => setRatio('day_change_max', event.target.value)} /><span>%</span></div>
          </Field>
          <Field label="上市最少交易日" hint="L2 上市不足否决">
            <input type="number" min="1" step="1" value={config.min_listing_sessions} onChange={(event) => patchConfig({ min_listing_sessions: Math.max(1, Number(event.target.value) || 1) })} />
          </Field>
          <Field label="L4 深筛上限" hint="最多逐只做财务深筛">
            <input type="number" min="0" step="1" value={config.max_deep_checks} onChange={(event) => patchConfig({ max_deep_checks: Math.max(0, Number(event.target.value) || 0) })} />
          </Field>
          <label className="switch-row screener-switch">
            <span><strong>L2 财务深筛</strong><small>亏损且营收&lt;1亿 / 净资产为负</small></span>
            <input type="checkbox" checked={config.enable_fundamental_veto} onChange={(event) => patchConfig({ enable_fundamental_veto: event.target.checked })} />
            <span className="switch" />
          </label>
          <label className="switch-row screener-switch">
            <span><strong>叠加实时快照</strong><small>腾讯批量快照重算 20日与当日涨幅</small></span>
            <input type="checkbox" checked={includeRealtime} onChange={(event) => setIncludeRealtime(event.target.checked)} />
            <span className="switch" />
          </label>
        </div>
        <div className="screener-param-footer">
          <span className="screener-hint">
            首轮建议关闭实时快照，用最近收盘口径复现 L1 计数。
            {notes.length > 0 ? ` 后端说明：${notes[0]}` : ''}
          </span>
          <button className="button primary run-button" onClick={() => void runScreen()} disabled={running || config.boards.length === 0}>
            {running ? <><span className="button-spinner" />正在筛选</> : <><Play size={16} fill="currentColor" />运行选股</>}
          </button>
        </div>
      </Panel>

      {runLoading ? <Panel><Loading label="正在读取选股结果" /></Panel> : !run ? (
        <Panel className="screener-empty-panel">
          <EmptyState
            icon={<Filter size={26} />}
            title="等待第一轮选股"
            description="先更新全市场日线，再点击「运行选股」。L0 大盘门关闭时本轮候选为空，页面会提示空仓等待。"
            action={<div className="empty-steps"><span><b>1</b>回补日线</span><i /><span><b>2</b>运行选股</span><i /><span><b>3</b>带入回测/模拟盘</span></div>}
          />
        </Panel>
      ) : (
        <>
          <Panel
            className="screener-gate-panel"
            title="L0 大盘同步门"
            description="近 20 日指数收益 ret20 < 0 视为开门；两板块都关门则本轮空仓等待"
            action={<Badge tone={run.gate.open ? 'green' : 'amber'}>{run.gate.open ? '大盘门开启' : '大盘门关闭 · 空仓等待'}</Badge>}
          >
            <div className="screener-gate-grid">
              {Object.entries(run.gate.boards).map(([board, gate]) => (
                <div className="screener-gate-card" key={board}>
                  <div className="screener-gate-head">
                    <div><strong>{board}</strong><small>{gate.index} · {shortDate(gate.date)}</small></div>
                    <Badge tone={gate.open ? 'green' : 'amber'}>{gate.open ? '开门' : '关门'}</Badge>
                  </div>
                  <div className={`screener-gate-ret ${toneOf(gate.ret20)}`}>
                    {signedPercent(gate.ret20)}
                    <small>近 20 日指数收益</small>
                  </div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel
            className="screener-funnel-panel"
            title="五层漏斗"
            description="universe → L0 大盘门 → L1 超跌 → L2 否决 → L3 容量 → L4 深筛"
            action={<span className="screener-run-meta">as_of {shortDate(run.as_of)} · 耗时 {number(run.elapsed_seconds, 2)}s · 深筛 {integer(run.funnel.deep_checked)} 只</span>}
          >
            <div className="screener-funnel-grid">
              {funnelStages.map((stage, index, stages) => {
                const previous = index === 0 ? stage.value : stages[index - 1].value;
                const keep = previous > 0 ? stage.value / previous : 0;
                const width = index === 0 ? 100 : Math.max(keep * 100, stage.value > 0 ? 4 : 0);
                return (
                  <div className="screener-funnel-stage" key={stage.tag}>
                    <div className="screener-funnel-top"><span className="screener-funnel-tag">{stage.tag}</span><strong>{integer(stage.value)}</strong></div>
                    <div className="screener-funnel-label">{stage.label}</div>
                    <div className="screener-funnel-bar"><i style={{ width: `${width}%` }} /></div>
                    <div className="screener-funnel-drop">
                      {index === 0 ? '起点' : `留存 ${percent(keep, 1)} · 淘汰 ${integer(Math.max(previous - stage.value, 0))}`}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="screener-data-info">
              <span>数据源 {String(run.data_info.universe_db || 'universe.db')}</span>
              <span>实时快照 {run.data_info.realtime ? `已叠加 ${integer(run.data_info.realtime_quotes ?? 0)} 只` : '未叠加'}</span>
              <span>财务深筛 {fundamentalLabel(run.data_info.fundamental)}</span>
              <span>pipeline 耗时 {number(run.elapsed_seconds, 2)} 秒</span>
            </div>
            {run.warnings.length > 0 ? (
              <div className="warning-box">
                <AlertTriangle size={18} />
                <div><strong>本轮警告</strong><ul>{run.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>
              </div>
            ) : <div className="no-warning"><AlertTriangle size={14} />后端未返回额外警告；L2 未覆盖项仍需人工复核。</div>}
          </Panel>

          <Panel
            className="screener-first-panel"
            title="第一名"
            description="L4 按 Chg20D 升序，财务深筛通过后的首位候选"
            action={first ? <Badge tone="indigo">FIRST PICK</Badge> : <Badge tone="amber">无可带入标的</Badge>}
          >
            {!first ? (
              <EmptyState
                compact
                icon={<Filter size={22} />}
                title="本轮没有可用第一名"
                description="可能是 L0 关门、L2 全部否决或 L4 深筛逐只否决；请查看候选池与 warnings。"
              />
            ) : (
              <div className="screener-first-card">
                <div className="screener-first-id">
                  <div className="screener-first-code"><strong>{displaySymbol(first.code)}</strong><span>{first.code}</span></div>
                  <div className="screener-first-name">
                    <h3>{first.name}</h3>
                    <div>
                      <Badge tone="indigo">{first.board}</Badge>
                      <Badge>收盘 {money(first.close)}</Badge>
                      <Badge tone="green">深筛通过</Badge>
                    </div>
                  </div>
                </div>
                <div className="screener-first-metrics">
                  <div><small>Chg20D</small><strong className={toneOf(first.chg20d)}>{signedPercent(first.chg20d)}</strong></div>
                  <div><small>距52周高</small><strong className="negative">{first.drawdown_52w > 0 ? `-${percent(first.drawdown_52w)}` : '—'}</strong></div>
                  <div><small>当日涨幅</small><strong className={toneOf(first.day_change)}>{signedPercent(first.day_change)}</strong></div>
                  <div><small>成交额</small><strong>{yi(first.amount)}</strong></div>
                </div>
                <div className="screener-first-actions">
                  <button className="button primary" disabled={preparing !== null} onClick={() => void bringIn('backtest', first)}>
                    {preparing === `backtest:${first.code}` ? <><span className="button-spinner" />带入中</> : <><Layers3 size={15} />带入回测</>}
                  </button>
                  <button
                    className="button secondary"
                    disabled={preparing !== null}
                    title="带入模拟盘（开启轮动：每次清仓后重新选股）"
                    aria-label="带入模拟盘（开启轮动：每次清仓后重新选股）"
                    onClick={() => void bringIn('paper', first)}
                  >
                    {preparing === `paper:${first.code}` ? <><span className="button-spinner" />带入中</> : <><Play size={15} />带入模拟盘</>}
                  </button>
                </div>
              </div>
            )}
          </Panel>

          <Panel
            className="screener-pool-panel"
            title="候选池"
            description="通过 L0–L3 的票按 Chg20D 升序；L4 深筛否决者保留在池内供复核"
            action={<Badge>{run.pool.length} 只在池 · {run.eligible.length} 只可用</Badge>}
          >
            <div className="table-scroll">
              <table className="screener-pool-table">
                <thead>
                  <tr>
                    <th>#</th><th>代码</th><th>名称</th><th>板块</th>
                    <th className="num">Chg20D</th><th className="num">距52周高</th><th className="num">当日涨幅</th>
                    <th className="num">成交额</th><th className="num">20日均额</th><th className="num">换手</th>
                    <th>状态</th><th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {run.pool.length === 0 ? <TableEmpty colSpan={12} message="本轮无候选：L0 关门或 L1/L2/L3 全部被过滤" /> : run.pool.map((candidate, index) => (
                    <tr key={candidate.code} className={candidate.vetoed_by ? 'screener-row-vetoed' : ''}>
                      <td><strong className="round-id">{String(index + 1).padStart(2, '0')}</strong></td>
                      <td><strong title={candidate.code}>{displaySymbol(candidate.code)}</strong></td>
                      <td>{candidate.name}</td>
                      <td><Badge tone="indigo">{candidate.board}</Badge></td>
                      <td className={`num ${toneOf(candidate.chg20d)}`}>{signedPercent(candidate.chg20d)}</td>
                      <td className="num negative">{candidate.drawdown_52w > 0 ? `-${percent(candidate.drawdown_52w)}` : '—'}</td>
                      <td className={`num ${toneOf(candidate.day_change)}`}>{signedPercent(candidate.day_change)}</td>
                      <td className="num">{yi(candidate.amount)}</td>
                      <td className="num">{yi(candidate.avg_amount_20d)}</td>
                      <td className="num">{percent(candidate.turnover)}</td>
                      <td
                        className="screener-status-cell"
                        title={candidate.checks.map((check) => `${check.layer} ${check.rule}：${check.status}${check.detail ? `（${check.detail}）` : ''}`).join('\n')}
                      >
                        {candidate.vetoed_by ? (
                          <><Badge tone="red">被否</Badge><small className="screener-reason">{candidate.vetoed_by}</small></>
                        ) : candidate.deep_checked ? (
                          <Badge tone="green">可用</Badge>
                        ) : (
                          <><Badge tone="amber">深度未查</Badge><small className="screener-reason">超出深筛上限 {run.config.max_deep_checks} 只</small></>
                        )}
                      </td>
                      <td>
                        {candidate.deep_checked && !candidate.vetoed_by ? (
                          <div className="screener-row-actions">
                            <button className="screener-link-button" disabled={preparing !== null} onClick={() => void bringIn('backtest', candidate)}>
                              {preparing === `backtest:${candidate.code}` ? '带入中' : '回测'}
                            </button>
                            <button
                              className="screener-link-button"
                              disabled={preparing !== null}
                              title="带入模拟盘（开启轮动：每次清仓后重新选股）"
                              onClick={() => void bringIn('paper', candidate)}
                            >
                              {preparing === `paper:${candidate.code}` ? '带入中' : '模拟盘'}
                            </button>
                          </div>
                        ) : <span className="muted" title="仅 L4 深筛通过的可用候选可带入回测或模拟盘">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="screener-pool-footnote">
              <span><i className="screener-dot green" />可用 = 已通过 L4 财务深筛，可进入轮动池</span>
              <span><i className="screener-dot red" />被否 = L4 财务深筛否决（原因见状态列）</span>
              <span><i className="screener-dot amber" />深度未查 = 超出本轮深筛上限，仅供观察</span>
              <span>仅「可用」行可一键带入回测/模拟盘</span>
            </div>
          </Panel>
        </>
      )}

      <Panel
        className="screener-history-panel"
        title="历史运行"
        description="每次运行的完整结果都会落库，可回看或删除"
        action={<History size={18} />}
      >
        <div className="table-scroll">
          <table>
            <thead><tr><th>运行时间</th><th>as_of</th><th>L0 门</th><th className="num">可用数</th><th>第一名</th><th>操作</th></tr></thead>
            <tbody>
              {history.length === 0 ? <TableEmpty colSpan={6} message="暂无历史运行，点击「运行选股」开始第一轮" /> : history.map((item) => (
                <tr key={item.id} className={run?.id === item.id ? 'screener-history-current' : ''}>
                  <td><strong>{dateTime(item.created_at)}</strong><small className="cell-code">{item.id}</small></td>
                  <td>{shortDate(item.as_of)}</td>
                  <td><Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge></td>
                  <td className="num">{integer(item.eligible_count)}</td>
                  <td>
                    {item.first ? (
                      <span className="screener-history-first">
                        <strong>{displaySymbol(item.first.code)}</strong>
                        {item.first.name}
                        <small className={toneOf(item.first.chg20d)}>{signedPercent(item.first.chg20d)}</small>
                      </span>
                    ) : <span className="muted">—</span>}
                  </td>
                  <td>
                    <div className="screener-row-actions">
                      <button className="screener-link-button" onClick={() => void loadRun(item.id)} disabled={deletingId === item.id}>查看</button>
                      <button className="screener-link-button danger" onClick={() => void removeRun(item.id)} disabled={deletingId === item.id}>
                        {deletingId === item.id ? '删除中' : '删除'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="screener-disclosure">
        <Filter size={14} />
        <span>选股结果由本地后端基于缓存日线与可选实时快照计算，不构成投资建议；L0 关门时应空仓等待，不降格选票。</span>
      </div>
    </div>
  );
}
