import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, CalendarDays, Clock3, Database, Filter, History, Layers3, Loader2, Play, RefreshCw,
} from 'lucide-react';
import { toast } from 'sonner';
import { ApiError, api } from '../lib/api';
import { dateTime, displaySymbol, integer, money, number, percent, shortDate, signedPercent } from '../lib/format';
import { Badge, EmptyState, ErrorBanner, Field, FormGrid, Loading, PageHeader, Panel, TableEmpty } from '../components/Ui';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { cn } from '@/lib/utils';
import type {
  RefreshStatus, ScreenerCandidate, ScreenerConfig, ScreenerCoverage, ScreenerRunSummary, ScreeningRun,
} from '../types';

const BOARD_OPTIONS = ['创业板', '科创板'] as const;
const LIST_PREVIEW_LIMIT = 10;

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
const toneClass = (value: number) => (value >= 0 ? 'text-destructive' : 'text-success');
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

function UnitInput({ value, onChange, unit, min, step }: { value: number; onChange: (value: string) => void; unit: string; min?: number; step?: number }) {
  return (
    <div className="relative">
      <Input type="number" min={min} step={step} value={value} onChange={(event) => onChange(event.target.value)} className="pr-8" />
      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[11px] text-muted-foreground">{unit}</span>
    </div>
  );
}

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
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [poolExpanded, setPoolExpanded] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
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
        toast.success('全市场日线刷新完成，覆盖数据已更新。');
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
    try {
      const result = await api.runScreener({ include_realtime: includeRealtime, config });
      setRun(result);
      toast.success(`选股完成：池内 ${result.pool.length} 只，可用 ${result.eligible.length} 只，耗时 ${number(result.elapsed_seconds, 2)} 秒。`);
      if (result.data_info.coverage) setCoverage(result.data_info.coverage);
      const runs = await api.screenerRuns().catch(() => null);
      if (runs) setHistory(runs);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '选股运行失败，请检查数据覆盖与后端日志。');
    } finally {
      setRunning(false);
    }
  };

  // 带入模拟盘（开启轮动：每次清仓后重新选股）；旧的「带入回测」服务单标的回测，已随其下线。
  const bringIn = async (target: 'paper', candidate: ScreenerCandidate) => {
    if (!run) { setError('请先运行或回看一次选股结果。'); return; }
    setPreparing(`${target}:${candidate.code}`);
    setError('');
    try {
      const prepared = await api.prepareScreenerRun(run.id, {
        target, candidate_code: candidate.code, rotation: true,
      });
      sessionStorage.setItem('layerlab.pendingPaper', JSON.stringify({
        runId: run.id, candidate: prepared.candidate, accountId: prepared.account?.id ?? null,
      }));
      window.location.hash = '#/paper';
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '一键带入失败。');
      setPreparing(null);
    }
  };

  const performDelete = async () => {
    const id = pendingDeleteId;
    if (!id) return;
    setDeletingId(id);
    setError('');
    try {
      const result = await api.deleteScreenerRun(id);
      if (!result.deleted) setError('后端未找到该运行记录。');
      setHistory((current) => current.filter((item) => item.id !== id));
      if (run?.id === id) setRun(null);
      toast.success('运行记录已删除。');
      setPendingDeleteId(null);
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
    <div className="animate-[page-in_.22s_ease-out]">
      <PageHeader
        eyebrow="SCREENER / FUNNEL"
        title="选股漏斗"
        description="L0 大盘同步门 → L1 A1 超跌信号 → L2 一票否决 → L3 容量 → L4 排序取第一；全 A 双创日线落库后逐层收敛候选池。"
        actions={<>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}><RefreshCw className={loading ? 'animate-spin' : ''} />刷新状态</Button>
          <Button size="sm" onClick={() => void runScreen()} disabled={running || config.boards.length === 0}>
            {running ? <><Loader2 className="animate-spin" />正在选股</> : <><Play />运行选股</>}
          </Button>
        </>}
      />

      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}

      <Panel
        title="数据覆盖"
        description="全市场双创日线缓存（baostock 前复权）；回补任务在后台线程运行，可断点续跑"
        action={<Badge tone={coverage && coverage.missing_codes === 0 && coverage.stale_codes === 0 ? 'green' : 'amber'}>
          {coverage ? (coverage.missing_codes === 0 && coverage.stale_codes === 0 ? '覆盖完整' : '需回补') : '未读取'}
        </Badge>}
        className="mb-3"
      >
        {loading && !coverage ? <Loading label="正在读取全市场覆盖情况" /> : !coverage ? (
          <EmptyState compact icon={<Database className="size-6" />} title="尚未读取覆盖数据" description="后端 universe.db 覆盖信息不可用；请确认服务已启动后重试。" action={<Button variant="outline" size="sm" onClick={() => void load()}>重试</Button>} />
        ) : (
          <>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {[
                { icon: Database, tone: 'text-primary bg-accent', label: '覆盖代码', value: integer(coverage.codes), note: '双创 universe' },
                { icon: Layers3, tone: 'text-foreground bg-muted', label: '日线行数', value: integer(coverage.rows), note: `${shortDate(coverage.start)} → ${shortDate(coverage.end)}` },
                { icon: AlertTriangle, tone: 'text-warning bg-warning/10', label: '未回补票', value: integer(coverage.missing_codes), note: '有代码但无日线' },
                { icon: Clock3, tone: 'text-success bg-success/10', label: '过期票', value: integer(coverage.stale_codes), note: '最新日线落后全局交易日' },
              ].map(({ icon: Icon, tone, label, value, note }) => (
                <Card key={label} className="flex-row items-center gap-3 px-3.5 py-3 shadow-none">
                  <span className={`grid size-9 shrink-0 place-items-center rounded-lg ${tone}`}><Icon className="size-4" /></span>
                  <div className="min-w-0"><small className="block text-[11px] text-muted-foreground">{label}</small><strong className="block font-mono text-lg font-medium">{value}</strong><span className="block truncate text-[10px] text-muted-foreground">{note}</span></div>
                </Card>
              ))}
            </div>
            <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1"><CalendarDays className="size-3" />覆盖区间 {shortDate(coverage.start)} → {shortDate(coverage.end)}</span>
              <span className="inline-flex items-center gap-1"><Layers3 className="size-3" />板块 {boardText}</span>
              <span className="inline-flex items-center gap-1"><Database className="size-3" />财务缓存 {integer(coverage.fundamentals)}</span>
              <span className="inline-flex items-center gap-1"><Clock3 className="size-3" />最后更新 {dateTime(coverage.updated_at)}</span>
            </div>
            {refreshRunning ? (
              <div className="mt-3 rounded-lg border bg-muted/35 p-3" role="status" aria-live="polite">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2"><span className="size-2 animate-pulse rounded-full bg-primary" /><strong className="text-[13px]">正在刷新全市场日线</strong></div>
                    <small className="mt-1 block text-[11px] text-muted-foreground">{job?.message || '后台回补中，可离开本页，稍后回来查看进度'}</small>
                  </div>
                  <Badge tone="indigo">{integer(job?.done)} / {integer(job?.total)} · {refreshPercent}%</Badge>
                </div>
                <Progress value={refreshPercent} className="mt-2" />
                <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                  <span>当前 {job?.current || '—'}</span><span>已写入 {integer(job?.fetched_rows)} 行</span>
                  <span>跳过 {integer(job?.skipped)}</span><span className={job && job.errors.length > 0 ? 'text-destructive' : ''}>错误 {job ? job.errors.length : 0}</span>
                </div>
                {job && job.errors.length > 0 && <div className="mt-2 space-y-1 rounded-md bg-destructive/5 px-2 py-1 text-[10px] text-destructive">{job.errors.slice(-3).map((item, index) => <span key={index} className="block">{item}</span>)}</div>}
              </div>
            ) : (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => void startRefresh('full')} disabled={refreshing}><RefreshCw className={refreshing ? 'animate-spin' : ''} />更新全市场日线</Button>
                <Button variant="outline" size="sm" onClick={() => void startRefresh('bootstrap')} disabled={refreshing}><Database />bootstrap 元数据</Button>
                <span className="text-[10px] text-muted-foreground">增量更新；首次全量约 1900 票 × 1.2s ≈ 35–50 分钟，后台线程执行。</span>
              </div>
            )}
          </>
        )}
      </Panel>

      <Panel title="选股参数" description="阈值直接映射后端 ScreenerConfig；界面百分比与亿元会自动换算为小数" action={<Filter className="size-4" />} className="mb-3">
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-muted-foreground">板块</span>
          {BOARD_OPTIONS.map((board) => {
            const active = config.boards.includes(board);
            return (
              <button key={board} type="button" className={cn('rounded-md border px-2 py-1 text-[11px] transition-colors', active ? 'border-primary/30 bg-accent text-accent-foreground' : 'bg-card text-muted-foreground hover:bg-muted')} onClick={() => toggleBoard(board)} aria-pressed={active}>
                {board}<small className="ml-1 font-mono text-[9px] opacity-70">{board === '创业板' ? 'sz.399006' : 'sh.000688'}</small>
              </button>
            );
          })}
          <span className="text-[10px] text-muted-foreground">板块决定 L0 使用哪个指数；至少保留一个。</span>
        </div>
        <FormGrid>
          <Field label="A1 20日跌幅阈值" hint="Chg20D < 阈值"><UnitInput value={percentValue(config.chg20_max)} onChange={(value) => setRatio('chg20_max', value)} unit="%" step={1} /></Field>
          <Field label="距52周高回撤" hint="Close < High250 × (1 - 回撤)"><UnitInput value={percentValue(config.week52_drawdown)} onChange={(value) => setRatio('week52_drawdown', value)} unit="%" min={0} step={1} /></Field>
          <Field label="L3 当日成交额门槛" hint="当日 amount ≥"><UnitInput value={Number((config.min_amount / 1e8).toFixed(2))} onChange={(value) => setYi('min_amount', value)} unit="亿" min={0} step={0.5} /></Field>
          <Field label="L3 20日均额门槛" hint="20 日均成交额 ≥"><UnitInput value={Number((config.min_avg_amount_20d / 1e8).toFixed(2))} onChange={(value) => setYi('min_avg_amount_20d', value)} unit="亿" min={0} step={0.5} /></Field>
          <Field label="当日涨幅上限" hint="L2 超过即否决"><UnitInput value={percentValue(config.day_change_max)} onChange={(value) => setRatio('day_change_max', value)} unit="%" step={1} /></Field>
          <Field label="上市最少交易日" hint="L2 上市不足否决"><Input type="number" min={1} step={1} value={config.min_listing_sessions} onChange={(event) => patchConfig({ min_listing_sessions: Math.max(1, Number(event.target.value) || 1) })} /></Field>
          <Field label="L4 深筛上限" hint="最多逐只做财务深筛"><Input type="number" min={0} step={1} value={config.max_deep_checks} onChange={(event) => patchConfig({ max_deep_checks: Math.max(0, Number(event.target.value) || 0) })} /></Field>
          <label className="flex w-full items-center justify-between gap-3 rounded-lg border bg-muted/35 px-2.5 py-2 sm:w-[240px] sm:self-start">
            <span><strong className="block text-[12px] font-medium">L2 财务深筛</strong><small className="block text-[10px] text-muted-foreground">亏损且营收&lt;1亿 / 净资产为负</small></span>
            <Switch checked={config.enable_fundamental_veto} onCheckedChange={(checked) => patchConfig({ enable_fundamental_veto: checked })} />
          </label>
          <label className="flex w-full items-center justify-between gap-3 rounded-lg border bg-muted/35 px-2.5 py-2 sm:w-[240px] sm:self-start">
            <span><strong className="block text-[12px] font-medium">叠加实时快照</strong><small className="block text-[10px] text-muted-foreground">腾讯批量快照重算 20日与当日涨幅</small></span>
            <Switch checked={includeRealtime} onCheckedChange={setIncludeRealtime} />
          </label>
        </FormGrid>
        <div className="mt-3 flex flex-col gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-[10px] leading-4 text-muted-foreground">首轮建议关闭实时快照，用最近收盘口径复现 L1 计数。{notes.length > 0 ? ` 后端说明：${notes[0]}` : ''}</span>
          <Button size="sm" onClick={() => void runScreen()} disabled={running || config.boards.length === 0}>
            {running ? <><Loader2 className="animate-spin" />正在筛选</> : <><Play />运行选股</>}
          </Button>
        </div>
      </Panel>

      {runLoading ? <Panel><Loading label="正在读取选股结果" /></Panel> : !run ? (
        <Panel className="mb-3">
          <EmptyState icon={<Filter className="size-6" />} title="等待第一轮选股" description="先更新全市场日线，再点击「运行选股」。L0 大盘门关闭时本轮候选为空，页面会提示空仓等待。" action={<div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground"><span className="rounded-md bg-muted px-2 py-1"><b className="mr-1 font-mono text-foreground">1</b>回补日线</span><span>→</span><span className="rounded-md bg-muted px-2 py-1"><b className="mr-1 font-mono text-foreground">2</b>运行选股</span><span>→</span><span className="rounded-md bg-muted px-2 py-1"><b className="mr-1 font-mono text-foreground">3</b>带入模拟盘</span></div>} />
        </Panel>
      ) : (
        <>
          <Panel title="L0 大盘同步门" description="近 20 日指数收益 ret20 < 0 视为开门；两板块都关门则本轮空仓等待" action={<Badge tone={run.gate.open ? 'green' : 'amber'}>{run.gate.open ? '大盘门开启' : '大盘门关闭 · 空仓等待'}</Badge>} className="mb-3">
            <div className="grid gap-2.5 sm:grid-cols-2">
              {Object.entries(run.gate.boards).map(([board, gate]) => (
                <div key={board} className={cn('rounded-xl border p-3', gate.open ? 'border-success/25 bg-success/5' : 'border-warning/25 bg-warning/5')}>
                  <div className="flex items-start justify-between gap-2">
                    <div><strong className="block text-[13px]">{board}</strong><small className="block font-mono text-[10px] text-muted-foreground">{gate.index} · {shortDate(gate.date)}</small></div>
                    <Badge tone={gate.open ? 'green' : 'amber'}>{gate.open ? '开门' : '关门'}</Badge>
                  </div>
                  <div className={cn('mt-2 font-mono text-xl font-medium', toneClass(gate.ret20))}>{signedPercent(gate.ret20)}<small className="ml-2 font-sans text-[10px] font-normal text-muted-foreground">近 20 日指数收益</small></div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="五层漏斗" description="universe → L0 大盘门 → L1 超跌 → L2 否决 → L3 容量 → L4 深筛" action={<span className="text-[10px] text-muted-foreground">as_of {shortDate(run.as_of)} · 耗时 {number(run.elapsed_seconds, 2)}s · 深筛 {integer(run.funnel.deep_checked)} 只</span>} className="mb-3">
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
              {funnelStages.map((stage, index, stages) => {
                const previous = index === 0 ? stage.value : stages[index - 1].value;
                const keep = previous > 0 ? stage.value / previous : 0;
                const width = index === 0 ? 100 : Math.max(keep * 100, stage.value > 0 ? 4 : 0);
                return (
                  <div key={stage.tag} className="flex flex-col gap-1.5 rounded-lg border bg-card p-2.5">
                    <div className="flex items-center justify-between"><span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">{stage.tag}</span><strong className="font-mono text-[15px]">{integer(stage.value)}</strong></div>
                    <div className="text-[11px] text-muted-foreground">{stage.label}</div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted"><i className="block h-full rounded-full bg-primary transition-all" style={{ width: `${width}%` }} /></div>
                    <div className="text-[10px] text-muted-foreground">{index === 0 ? '起点' : `留存 ${percent(keep, 1)} · 淘汰 ${integer(Math.max(previous - stage.value, 0))}`}</div>
                  </div>
                );
              })}
            </div>
            <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1.5 rounded-lg bg-muted/50 px-3 py-2 text-[10px] text-muted-foreground">
              <span>数据源 {String(run.data_info.universe_db || 'universe.db')}</span>
              <span>实时快照 {run.data_info.realtime ? `已叠加 ${integer(run.data_info.realtime_quotes ?? 0)} 只` : '未叠加'}</span>
              <span>财务深筛 {fundamentalLabel(run.data_info.fundamental)}</span>
              <span>pipeline 耗时 {number(run.elapsed_seconds, 2)} 秒</span>
            </div>
            {run.warnings.length > 0 ? (
              <div className="mt-2.5 flex items-start gap-2 rounded-lg border border-warning/25 bg-warning/5 px-3 py-2 text-[11px] text-warning">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                <div><strong className="block">本轮警告</strong><ul className="mt-1 list-disc space-y-0.5 pl-4">{run.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>
              </div>
            ) : <div className="mt-2.5 flex items-center gap-1.5 text-[10px] text-muted-foreground"><AlertTriangle className="size-3.5" />后端未返回额外警告；L2 未覆盖项仍需人工复核。</div>}
          </Panel>

          <Panel title="第一名" description="L4 按 Chg20D 升序，财务深筛通过后的首位候选" action={first ? <Badge tone="indigo">FIRST PICK</Badge> : <Badge tone="amber">无可带入标的</Badge>} className="mb-3">
            {!first ? (
              <EmptyState compact icon={<Filter className="size-6" />} title="本轮没有可用第一名" description="可能是 L0 关门、L2 全部否决或 L4 深筛逐只否决；请查看候选池与 warnings。" />
            ) : (
              <div className="grid gap-2 lg:grid-cols-[1fr_auto] lg:items-center">
                <div className="flex flex-col gap-2.5">
                  <div className="flex items-center gap-3">
                    <div className="rounded-lg bg-accent px-3 py-2 text-center"><strong className="block font-mono text-xl text-accent-foreground">{displaySymbol(first.code)}</strong><span className="block font-mono text-[10px] text-muted-foreground">{first.code}</span></div>
                    <div><h3 className="text-base font-semibold">{first.name}</h3><div className="mt-1 flex flex-wrap gap-1.5"><Badge tone="indigo">{first.board}</Badge><Badge>收盘 {money(first.close)}</Badge><Badge tone="green">深筛通过</Badge></div></div>
                  </div>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    {[
                      ['Chg20D', signedPercent(first.chg20d), toneClass(first.chg20d)],
                      ['距52周高', first.drawdown_52w > 0 ? `-${percent(first.drawdown_52w)}` : '—', 'text-success'],
                      ['当日涨幅', signedPercent(first.day_change), toneClass(first.day_change)],
                      ['成交额', yi(first.amount), ''],
                    ].map(([label, value, className]) => (
                      <div key={String(label)} className="rounded-lg border bg-muted/30 px-2.5 py-2"><small className="block text-[10px] text-muted-foreground">{label}</small><strong className={cn('block font-mono text-[13px]', className)}>{value}</strong></div>
                    ))}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" disabled={preparing !== null} title="带入模拟盘（开启轮动：每次清仓后重新选股）" aria-label="带入模拟盘（开启轮动：每次清仓后重新选股）" onClick={() => void bringIn('paper', first)}>
                    {preparing === `paper:${first.code}` ? <><Loader2 className="animate-spin" />带入中</> : <><Play />带入模拟盘</>}
                  </Button>
                </div>
              </div>
            )}
          </Panel>

          <Panel title="候选池" description="通过 L0–L3 的票按 Chg20D 升序；L4 深筛否决者保留在池内供复核" action={<Badge>{run.pool.length} 只在池 · {run.eligible.length} 只可用</Badge>} flush className="mb-3">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>#</TableHead><TableHead>代码</TableHead><TableHead>名称</TableHead><TableHead>板块</TableHead>
                  <TableHead className="text-right">Chg20D</TableHead><TableHead className="text-right">距52周高</TableHead><TableHead className="text-right">当日涨幅</TableHead>
                  <TableHead className="text-right">成交额</TableHead><TableHead className="text-right">20日均额</TableHead><TableHead className="text-right">换手</TableHead>
                  <TableHead>状态</TableHead><TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {run.pool.length === 0 ? <TableEmpty colSpan={12} message="本轮无候选：L0 关门或 L1/L2/L3 全部被过滤" /> : (poolExpanded ? run.pool : run.pool.slice(0, LIST_PREVIEW_LIMIT)).map((candidate, index) => (
                  <TableRow key={candidate.code} className={candidate.vetoed_by ? 'bg-muted/30' : ''}>
                    <TableCell><strong className="font-mono text-[11px]">{String(index + 1).padStart(2, '0')}</strong></TableCell>
                    <TableCell><strong className="font-mono text-[12px]" title={candidate.code}>{displaySymbol(candidate.code)}</strong></TableCell>
                    <TableCell>{candidate.name}</TableCell>
                    <TableCell><Badge tone="indigo">{candidate.board}</Badge></TableCell>
                    <TableCell className={cn('text-right font-mono text-[12px]', toneClass(candidate.chg20d))}>{signedPercent(candidate.chg20d)}</TableCell>
                    <TableCell className="text-right font-mono text-[12px] text-success">{candidate.drawdown_52w > 0 ? `-${percent(candidate.drawdown_52w)}` : '—'}</TableCell>
                    <TableCell className={cn('text-right font-mono text-[12px]', toneClass(candidate.day_change))}>{signedPercent(candidate.day_change)}</TableCell>
                    <TableCell className="text-right font-mono text-[12px]">{yi(candidate.amount)}</TableCell>
                    <TableCell className="text-right font-mono text-[12px]">{yi(candidate.avg_amount_20d)}</TableCell>
                    <TableCell className="text-right font-mono text-[12px]">{percent(candidate.turnover)}</TableCell>
                    <TableCell title={candidate.checks.map((check) => `${check.layer} ${check.rule}：${check.status}${check.detail ? `（${check.detail}）` : ''}`).join('\n')}>
                      {candidate.vetoed_by ? <><Badge tone="red">被否</Badge><small className="mt-1 block max-w-[160px] truncate text-[10px] text-muted-foreground">{candidate.vetoed_by}</small></> : candidate.deep_checked ? <Badge tone="green">可用</Badge> : <><Badge tone="amber">深度未查</Badge><small className="mt-1 block text-[10px] text-muted-foreground">超出深筛上限 {run.config.max_deep_checks} 只</small></>}
                    </TableCell>
                    <TableCell>
                      {candidate.deep_checked && !candidate.vetoed_by ? (
                        <div className="flex items-center gap-1">
                          <button className="rounded px-1.5 py-0.5 text-[11px] text-primary hover:bg-accent" disabled={preparing !== null} title="带入模拟盘（开启轮动：每次清仓后重新选股）" onClick={() => void bringIn('paper', candidate)}>{preparing === `paper:${candidate.code}` ? '带入中' : '模拟盘'}</button>
                        </div>
                      ) : <span className="text-muted-foreground" title="仅 L4 深筛通过的可用候选可带入模拟盘">—</span>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {run.pool.length > LIST_PREVIEW_LIMIT && (
              <div className="flex items-center justify-between border-t px-4 py-2 text-[11px] text-muted-foreground">
                <span>候选池共 {run.pool.length} 只，默认展示前 {LIST_PREVIEW_LIMIT} 只</span>
                <button className="text-primary hover:underline" onClick={() => setPoolExpanded(!poolExpanded)}>{poolExpanded ? '收起列表' : `展开全部 ${run.pool.length} 只`}</button>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-4 py-2 text-[10px] text-muted-foreground">
              <span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-success" />可用 = 已通过 L4 财务深筛</span>
              <span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-destructive" />被否 = L4 财务深筛否决</span>
              <span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-warning" />深度未查 = 超出本轮深筛上限</span>
              <span>仅「可用」行可一键带入模拟盘</span>
            </div>
          </Panel>
        </>
      )}

      <Panel title="历史运行" description="每次运行的完整结果都会落库，可回看或删除" action={<History className="size-4" />} flush>
        <Table>
          <TableHeader><TableRow><TableHead>运行时间</TableHead><TableHead>as_of</TableHead><TableHead>L0 门</TableHead><TableHead className="text-right">可用数</TableHead><TableHead>第一名</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
          <TableBody>
            {history.length === 0 ? <TableEmpty colSpan={6} message="暂无历史运行，点击「运行选股」开始第一轮" /> : (historyExpanded ? history : history.slice(0, LIST_PREVIEW_LIMIT)).map((item) => (
              <TableRow key={item.id} className={run?.id === item.id ? 'bg-accent/40' : ''}>
                <TableCell><strong className="block text-[12px]">{dateTime(item.created_at)}</strong><small className="block font-mono text-[10px] text-muted-foreground">{item.id}</small></TableCell>
                <TableCell>{shortDate(item.as_of)}</TableCell>
                <TableCell><Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge></TableCell>
                <TableCell className="text-right font-mono text-[12px]">{integer(item.eligible_count)}</TableCell>
                <TableCell>
                  {item.first ? <span className="inline-flex items-center gap-1.5 text-[12px]"><strong className="font-mono text-[11px]">{displaySymbol(item.first.code)}</strong>{item.first.name}<small className={toneClass(item.first.chg20d)}>{signedPercent(item.first.chg20d)}</small></span> : <span className="text-muted-foreground">—</span>}
                </TableCell>
                <TableCell>
                  <div className="flex items-center gap-1">
                    <button className="rounded px-1.5 py-0.5 text-[11px] text-primary hover:bg-accent" onClick={() => void loadRun(item.id)} disabled={deletingId === item.id}>查看</button>
                    <button className="rounded px-1.5 py-0.5 text-[11px] text-destructive hover:bg-destructive/10" onClick={() => setPendingDeleteId(item.id)} disabled={deletingId === item.id}>{deletingId === item.id ? '删除中' : '删除'}</button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {history.length > LIST_PREVIEW_LIMIT && (
          <div className="flex items-center justify-between border-t px-4 py-2 text-[11px] text-muted-foreground">
            <span>历史运行共 {history.length} 次，默认展示最近 {LIST_PREVIEW_LIMIT} 次</span>
            <button className="text-primary hover:underline" onClick={() => setHistoryExpanded(!historyExpanded)}>{historyExpanded ? '收起列表' : `展开全部 ${history.length} 次`}</button>
          </div>
        )}
      </Panel>

      <div className="mt-3 flex items-start gap-2 text-[11px] leading-5 text-muted-foreground"><Filter className="mt-0.5 size-3.5 shrink-0" /><span>选股结果由本地后端基于缓存日线与可选实时快照计算，不构成投资建议；L0 关门时应空仓等待，不降格选票。</span></div>

      <ConfirmDialog
        open={pendingDeleteId !== null}
        onOpenChange={(open) => { if (!open) setPendingDeleteId(null); }}
        title="删除选股运行记录"
        description="删除后无法恢复。该操作只删除本次选股运行记录，不影响回测报告与模拟账户。"
        confirmLabel="删除"
        destructive
        busy={deletingId !== null}
        onConfirm={performDelete}
      />
    </div>
  );
}

