import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowDownLeft, ArrowUpRight, CircleDollarSign, FastForward, Landmark, Loader2, Pause, Play,
  Plus, Radio, RefreshCw, Repeat2, RotateCcw, Send, ShieldCheck, StepForward, Timer, Wallet, XCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { ApiError, api } from '../lib/api';
import { cleanSymbolInput, dateTime, displaySymbol, integer, lower, money, number, orderTypeLabel, purposeLabel, shortDate, sideLabel, signedMoney, signedPercent, statusLabel } from '../lib/format';
import FeeConfigPanel from '../components/FeeConfigPanel';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { Badge, EmptyState, ErrorBanner, Field, FormGrid, Loading, Metric, MetricGrid, PageHeader, Panel, SelectField, SwitchRow, SyntheticNotice, TableEmpty } from '../components/Ui';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NumberInput } from '@/components/ui/number-input';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import { defaultFeeConfig, feeBreakdownTitle, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import type { AccountState, BoardRules, Dataset, FeeConfig, OrderPurpose, OrderSide, OrderType, PositionQuote, RotationPoolItem, SchedulerStatus, StrategyConfig, TradingRulesResponse, TradingSession } from '../types';

const POOL_PREVIEW_LIMIT = 10;

// 自动同步窗口（本地时间）：09:25 起、15:30 停（收盘后不再轮询）。
// 后端定时器本身只在 09:30–11:30 / 13:00–15:00 成交推进；15:30 留出收尾观察期。
const AUTO_SYNC_START_MINUTES = 9 * 60 + 25;
const AUTO_SYNC_END_MINUTES = 15 * 60 + 30;
const AUTO_SYNC_INTERVAL_MS = 15000;

const minutesOfDay = (date: Date): number => date.getHours() * 60 + date.getMinutes();

const localDateText = (date: Date): string =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

/** 当前是否处于「页面自动同步」时段：必须是交易日（后端日历判定）且 09:25–15:30。 */
const isAutoSyncWindow = (date: Date, isTradingDay: boolean): boolean =>
  isTradingDay
  && minutesOfDay(date) >= AUTO_SYNC_START_MINUTES
  && minutesOfDay(date) < AUTO_SYNC_END_MINUTES;

/**
 * 到下一次自动同步窗口开始的毫秒数。
 * 优先用后端给出的 next_trading_day（能跳过节假日）；拿不到时回落到「下一个工作日」。
 */
const msUntilAutoSyncResume = (
  date: Date, isTradingDay: boolean, nextTradingDay: string | null,
): number => {
  if (isTradingDay && minutesOfDay(date) < AUTO_SYNC_START_MINUTES) {
    const todayOpen = new Date(date);
    todayOpen.setHours(9, 25, 0, 0);
    return Math.max(1000, todayOpen.getTime() - date.getTime());
  }
  if (nextTradingDay) {
    const target = new Date(`${nextTradingDay}T09:25:00`);
    if (!Number.isNaN(target.getTime())) {
      return Math.max(1000, target.getTime() - date.getTime());
    }
  }
  const next = new Date(date);
  do {
    next.setDate(next.getDate() + 1);
  } while (next.getDay() === 0 || next.getDay() === 6);
  next.setHours(9, 25, 0, 0);
  return Math.max(1000, next.getTime() - date.getTime());
};
const AMOUNT_TIERS = [100_000, 200_000, 300_000, 400_000] as const;

const defaultConfig: StrategyConfig = {
  enable_t: true, commission_rate: 0.00005, min_commission: 5, stamp_tax_rate: 0.0005,
  handling_fee_rate: 0.0000341, regulatory_fee_rate: 0.00002, transfer_fee_rate: 0.00001,
  slippage_bps: 1, volume_participation: 0.2,
};

function Segmented({ options, value, onChange }: { options: Array<{ value: string; label: string }>; value: string; onChange: (value: string) => void }) {
  return (
    <div className="flex gap-0.5 rounded-lg bg-muted p-0.5">
      {options.map((option) => (
        <button key={option.value} type="button" onClick={() => onChange(option.value)} className={cn('rounded-md px-2.5 py-1 text-[11px] transition-colors', value === option.value ? 'bg-card text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground')}>{option.label}</button>
      ))}
    </div>
  );
}

export default function PaperPage() {
  const [account, setAccount] = useState<AccountState | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [config, setConfig] = useState<StrategyConfig>(defaultConfig);
  const [fees, setFees] = useState<FeeConfig>(defaultFeeConfig);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pendingReset, setPendingReset] = useState(false);
  const [accountName, setAccountName] = useState('本地模拟账户');
  const [initialCash, setInitialCash] = useState(4000000);
  const [datasetId, setDatasetId] = useState('');
  const [autoStrategy, setAutoStrategy] = useState(false);
  const [togglingAuto, setTogglingAuto] = useState(false);
  const [togglingRotation, setTogglingRotation] = useState(false);
  const [creating, setCreating] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [playing, setPlaying] = useState(false);
  const advancingRef = useRef(false);
  const [liveLoading, setLiveLoading] = useState(false);
  // 后端定时器状态 + 最近一次页面同步时间：让「成交已发生」在页面上可见。
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [lastSync, setLastSync] = useState('');
  /** 收盘/非交易时段：自动同步已暂停（不再发起轮询请求）。 */
  const [autoSyncPaused, setAutoSyncPaused] = useState(false);
  /** 后端交易日历判定：周末/节假日不轮询。 */
  const [tradingSession, setTradingSession] = useState<TradingSession | null>(null);
  // 记录已知成交笔数：轮询发现新增成交时给一条提示，避免「成交了但页面没反应」。
  const knownTradeCountRef = useRef<number | null>(null);
  const [side, setSide] = useState<OrderSide>('buy');
  const [purpose, setPurpose] = useState<OrderPurpose>('PERMANENT');
  const [orderType, setOrderType] = useState<OrderType>('market');
  const [symbol, setSymbol] = useState('');
  const [quantity, setQuantity] = useState(100);
  const [limitPrice, setLimitPrice] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState<string | number | null>(null);
  const [resetting, setResetting] = useState(false);
  const [recordTab, setRecordTab] = useState<'orders' | 'trades'>('orders');
  // 分板块交易规则：按 symbol 实时拉取，驱动快捷档位 / 默认数量 / 实时校验。
  const [tradingRules, setTradingRules] = useState<TradingRulesResponse | null>(null);
  const [allBoards, setAllBoards] = useState<BoardRules[]>([]);
  const [rulesView, setRulesView] = useState<'current' | 'all'>('current');
  const [rulesLoading, setRulesLoading] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [qtyError, setQtyError] = useState('');
  const [tierAmount, setTierAmount] = useState<number | null>(null);
  const [tierNote, setTierNote] = useState('');
  const [tierError, setTierError] = useState(false);
  const [tierPicking, setTierPicking] = useState<number | null>(null);
  const [showAllPool, setShowAllPool] = useState(false);

  // 池内轮动（v2）：清仓后重新跑五层选股取当时第一名；live-tick 优先使用当前标的。
  const rotation = account?.rotation ?? null;
  const rotationCurrent = rotation?.current_symbol || '';
  const liveCode = cleanSymbolInput(rotationCurrent || symbol);
  const lastScreen = rotation?.last_screen ?? null;
  const lastScreenFirst = lastScreen?.first ?? null;
  const lastAttempt = rotation?.last_attempt_date || lastScreen?.as_of || '';
  const lastRunId = lastScreen?.run_id || rotation?.last_run_id || rotation?.run_id || '';
  const rotationCandidates: RotationPoolItem[] = rotation?.pool?.length
    ? rotation.pool
    : lastScreenFirst
      ? [{ symbol: lastScreenFirst.code, name: lastScreenFirst.name, chg20d: lastScreenFirst.chg20d ?? undefined }]
      : [];

  // 持仓表只认真实持仓：后端 positions 已过滤零持仓，这里再兜底一次，
  // 避免旧后端把「只有最新价」的行情缓存也塞进 positions 时出现 0 股幽灵持仓行。
  const holdingPositions = useMemo(
    () => (account?.positions ?? []).filter((position) => position.total_qty > 0),
    [account],
  );
  const heldQtyBySymbol = useMemo(
    () => new Map(holdingPositions.map((position) => [position.symbol, position.total_qty])),
    [holdingPositions],
  );
  // 行情列表 = 后端 quotes + 仍持仓但旧后端未返回 quotes 的兜底标的。
  const quoteRows = useMemo<PositionQuote[]>(() => {
    const rows: PositionQuote[] = [...(account?.quotes ?? [])];
    const seen = new Set(rows.map((quote) => quote.symbol));
    for (const position of account?.positions ?? []) {
      if (!seen.has(position.symbol)) rows.push({ symbol: position.symbol, last_price: position.last_price });
    }
    return rows;
  }, [account]);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [datasetResult, defaults] = await Promise.all([api.datasets(), api.strategyDefaults()]);
      setDatasets(datasetResult);
      const defaultConfigFromApi = defaults.config || defaults;
      setConfig((current) => ({ ...current, ...defaultConfigFromApi }));
      setFees(defaults.fees || strategyToFeeConfig(defaultConfigFromApi));
      const daily = datasetResult.find((item) => item.timeframe === 'daily') || datasetResult[0];
      if (daily) { setDatasetId((current) => current || daily.id); }
      try {
        const current = await api.currentAccount();
        setAccount(current);
        setAutoStrategy(Boolean(current.auto_strategy));
        if (current.dataset_id) setDatasetId(current.dataset_id);
        // 标的代码默认带出轮动当前标的（若不是刚从选股页带入其它候选）。
        const rotationCode = current.rotation?.current_symbol ? cleanSymbolInput(current.rotation.current_symbol) : '';
        if (rotationCode) setSymbol((prev) => prev || rotationCode);
        // 选股页「带入模拟盘」写入的提示：候选标的与运行来源。
        const pendingPaper = sessionStorage.getItem('layerlab.pendingPaper');
        let broughtIn = false;
        if (pendingPaper) {
          sessionStorage.removeItem('layerlab.pendingPaper');
          try {
            const pending = JSON.parse(pendingPaper) as { runId?: string; candidate?: { code?: string; name?: string } };
            const code = pending.candidate?.code ? cleanSymbolInput(pending.candidate.code) : '';
            if (code) { setSymbol(code); broughtIn = true; }
            const label = [code, pending.candidate?.name].filter(Boolean).join(' ');
            const rotationText = current.rotation?.enabled ? '，已自动开启轮动（每次清仓后重新选股）' : '';
            toast.success(`已从选股结果建立模拟账户：${label || '候选标的'}（运行 ${pending.runId || '—'}）${rotationText}。`);
          } catch {
            // 忽略损坏的 sessionStorage 数据
          }
        }
        if (!broughtIn && !rotationCode) {
          const held = current.positions.find((position) => position.total_qty > 0);
          if (held) setSymbol(cleanSymbolInput(held.symbol));
        }
      }
      catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 404)) throw cause;
        setAccount(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模拟账户读取失败。');
    } finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, []);

  // 六板块规则全表：进页面拉一次，供「全部板块」对比视图使用。
  useEffect(() => {
    let cancelled = false;
    api.tradingRulesAll()
      .then((data) => { if (!cancelled) setAllBoards(data.boards); })
      .catch(() => { /* 全表加载失败不阻塞下单，仅对比视图显示为空 */ });
    return () => { cancelled = true; };
  }, []);

  // 按标的拉取分板块交易规则：科创板/创业板/主板数量口径与涨跌幅不同，进页面即对齐。
  const rulesCode = cleanSymbolInput(symbol);
  useEffect(() => {
    if (rulesCode.length !== 6) { setTradingRules(null); setQtyError(''); return; }
    let cancelled = false;
    setRulesLoading(true);
    api.tradingRules(rulesCode)
      .then((data) => {
        if (cancelled) return;
        setTradingRules(data);
        // 默认数量自动对齐板块最小申报量：科创板进页面就是 200 股，不再一进来就报红。
        setQuantity((current) => (current < data.rules.min_qty ? data.rules.min_qty : current));
      })
      .catch(() => { if (!cancelled) setTradingRules(null); })
      .finally(() => { if (!cancelled) setRulesLoading(false); });
    return () => { cancelled = true; };
  }, [rulesCode]);

  // 快捷档位改为「金额」：按参考价与板块规则换算成股数，并校验最小申报/递增/单笔上限。
  const resetTier = () => {
    setTierAmount(null);
    setTierNote('');
    setTierError(false);
  };

  const applySymbol = (next: string) => {
    setSymbol(next);
    resetTier();
  };

  const referencePriceFor = (code: string) => {
    const quote = quoteRows.find((item) => cleanSymbolInput(item.symbol) === code && item.last_price > 0);
    return quote?.last_price ?? 0;
  };

  const applyAmountTier = async (amount: number) => {
    const code = cleanSymbolInput(symbol);
    if (!code) {
      setTierAmount(null);
      setTierError(true);
      setTierNote('请先填写标的代码，再选择快捷金额。');
      return;
    }
    const rules = tradingRules?.rules;
    if (!rules) {
      setTierError(true);
      setTierNote(rulesLoading ? '正在加载交易规则，请稍候再试。' : '交易规则尚未加载，请重新输入 6 位代码。');
      return;
    }
    let reference = 0;
    if (orderType === 'limit') {
      reference = Number(limitPrice);
      if (!Number.isFinite(reference) || reference <= 0) {
        setTierError(true);
        setTierNote('限价委托请先填写委托价格，再选择快捷金额。');
        return;
      }
    } else {
      reference = referencePriceFor(code);
      if (!reference) {
        setTierPicking(amount);
        try {
          const quote = await api.marketQuote(code);
          reference = Number(quote.price);
        } catch {
          setTierError(true);
          setTierNote('未获取到最新价：请先拉取实时快照，或切到「限价」填写委托价。');
          return;
        } finally {
          setTierPicking(null);
        }
      }
    }
    if (!Number.isFinite(reference) || reference <= 0) {
      setTierError(true);
      setTierNote('参考价无效，请先拉取实时快照或填写委托价。');
      return;
    }
    const step = Math.max(1, rules.step);
    const cap = orderType === 'limit' ? rules.limit_order_cap : rules.market_order_cap;
    let qty = Math.floor(amount / reference / step) * step;
    if (qty < rules.min_qty) qty = rules.min_qty;
    if (cap > 0) qty = Math.min(qty, Math.max(rules.min_qty, Math.floor(cap / step) * step));
    qty = Math.max(rules.min_qty, Math.floor(qty / step) * step);
    setQuantity(qty);
    setTierAmount(amount);
    setTierError(false);
    setTierNote(`${amount / 10_000} 万 ≈ ${integer(qty)} 股 · 参考价 ¥${reference.toFixed(2)}${cap > 0 && qty >= cap ? '（已按单笔上限截断）' : ''}`);
  };

  // 实时校验：数量符合板块口径才允许提交，并把具体原因直接展示。
  useEffect(() => {
    const rules = tradingRules?.rules;
    if (!rules || !Number.isInteger(quantity) || quantity <= 0) { setQtyError(''); return; }
    if (quantity < rules.min_qty) { setQtyError(`${rules.board}最小申报 ${rules.min_qty} 股，当前 ${quantity} 股`); return; }
    if (rules.step > 1 && quantity % rules.step) { setQtyError(`${rules.board}申报数量须为 ${rules.step} 股整数倍`); return; }
    const cap = rules.limit_order_cap;
    if (orderType === 'limit' && cap > 0 && quantity > cap) {
      setQtyError(`${rules.board}限价单笔上限 ${integer(cap)} 股，当前 ${integer(quantity)} 股`);
      return;
    }
    const marketCap = rules.market_order_cap;
    if (orderType === 'market' && marketCap > 0 && quantity > marketCap) {
      setQtyError(`${rules.board}市价单笔上限 ${integer(marketCap)} 股，当前 ${integer(quantity)} 股`);
      return;
    }
    setQtyError('');
  }, [tradingRules, quantity, orderType, limitPrice]);

  const refreshAccount = async (id: string, silent = false) => {
    if (!silent) setLoading(true);
    try {
      const current = await api.account(id);
      setAccount(current);
      setAutoStrategy(Boolean(current.auto_strategy));
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : '账户刷新失败。'); }
    finally { if (!silent) setLoading(false); }
  };

  const advance = async (bars: number, fromPlayer = false) => {
    if (!account || advancingRef.current) return;
    advancingRef.current = true;
    if (!fromPlayer) setAdvancing(true);
    setError('');
    try {
      const next = await api.advance(account.id, bars);
      setAccount(next);
      if (lower(next.status) !== 'active' && fromPlayer) setPlaying(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '行情推送失败。');
      if (fromPlayer) setPlaying(false);
    } finally {
      advancingRef.current = false;
      if (!fromPlayer) setAdvancing(false);
    }
  };

  useEffect(() => {
    if (!playing || !account) return;
    const timer = window.setInterval(() => void advance(1, true), 1400);
    return () => window.clearInterval(timer);
  }, [playing, account?.id]);

  const liveTick = async () => {
    const code = liveCode;
    if (!account || code.length !== 6) return;
    setLiveLoading(true);
    setError('');
    try {
      const next = await api.liveTick(account.id, code);
      setAccount(next);
      toast.success('已按腾讯实时快照刷新本地撮合账户。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '腾讯实时行情刷新失败。');
    } finally {
      setLiveLoading(false);
    }
  };

  // 成交推进由后端定时器负责（/api/paper/scheduler，交易时段每 5 分钟一轮）；
  // 页面只负责把结果同步上来，不参与撮合。
  //
  // 自动同步窗口：**交易日**（后端 quan 交易日历判定，周末/节假日不轮询）09:25–15:30；
  // 15:30 收盘后停止发请求，下一个交易日 09:25 自动恢复；标签页隐藏时同样暂停。
  // 只更新账户数据（委托/成交/持仓/轮动状态），不碰表单与回放等本地状态。
  const accountId = account?.id;
  useEffect(() => {
    if (!accountId) return;
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;
    let session: TradingSession | null = null;
    let sessionAt = 0;
    knownTradeCountRef.current = null;
    // quan 日历是权威数据，当天内可一直用；工作日回落/行情探测可能随开盘变化，
    // 只缓存 5 分钟（例如节假日 09:25 醒来时还探测不到，09:35 后要重新确认）。
    const SESSION_TTL_MS = 5 * 60 * 1000;

    const scheduleNext = (delay: number) => {
      if (cancelled) return;
      timer = window.setTimeout(() => void run(), delay);
    };

    const sync = async () => {
      if (cancelled || inFlight || document.hidden || advancingRef.current) return;
      inFlight = true;
      try {
        const [next, status] = await Promise.all([
          api.account(accountId),
          api.scheduler().catch(() => null),
        ]);
        if (cancelled) return;
        const tradeCount = next.trades.length;
        const known = knownTradeCountRef.current;
        if (known !== null && tradeCount > known) {
          const latest = next.trades[tradeCount - 1];
          toast.success(`后端已自动成交：${sideLabel(latest.side)} ${integer(latest.quantity)} 股 @ ${money(latest.price)}（${dateTime(latest.timestamp)}），页面已同步。`);
        }
        knownTradeCountRef.current = tradeCount;
        // 内容没变就不 setState，避免 15 秒一次的整页重渲染。
        setAccount((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
        if (status) {
          setScheduler(status);
          if (status.last_tick_at) setLastSync(status.last_tick_at.slice(11, 19));
        }
      } catch {
        /* 轮询失败静默：下一轮再试，不打断用户操作 */
      } finally {
        inFlight = false;
      }
    };

    /** 交易日判定：后端 quan 日历优先（能识别节假日），接口失败才回落「周一到周五」。 */
    const loadSession = async (now: Date): Promise<TradingSession | null> => {
      const day = localDateText(now);
      const ttl = session?.source?.includes('calendar') ? Number.POSITIVE_INFINITY : SESSION_TTL_MS;
      if (session && session.today === day && now.getTime() - sessionAt < ttl) return session;
      try {
        session = await api.tradingSession();
        sessionAt = now.getTime();
        if (!cancelled) setTradingSession(session);
      } catch {
        session = null;
      }
      return session;
    };

    const run = async () => {
      if (cancelled) return;
      const now = new Date();
      const info = await loadSession(now);
      const tradingNow = info ? info.is_trading_day : now.getDay() >= 1 && now.getDay() <= 5;
      if (!isAutoSyncWindow(now, tradingNow)) {
        // 收盘 / 周末 / 节假日：不发任何行情请求，直接睡到下一个交易日 09:25。
        setAutoSyncPaused(true);
        scheduleNext(msUntilAutoSyncResume(now, tradingNow, info?.next_trading_day ?? null));
        return;
      }
      setAutoSyncPaused(false);
      await sync();
      scheduleNext(AUTO_SYNC_INTERVAL_MS);
    };

    const onVisible = () => {
      if (document.hidden) return;
      if (timer !== undefined) window.clearTimeout(timer);
      void run();
    };
    document.addEventListener('visibilitychange', onVisible);
    void run();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [accountId]);

  const createAccount = async () => {
    if (!accountName.trim() || !Number.isFinite(initialCash) || initialCash <= 0) { setError('请填写账户名称，并确保初始资金大于 0。'); return; }
    setCreating(true); setError('');
    try {
      const next = await api.createAccount({
        name: accountName.trim(), initial_cash: initialCash,
        dataset_id: datasetId || undefined, auto_strategy: autoStrategy,
        config: stripFeeFields(config), fees,
      });
      setAccount(next);
      setAutoStrategy(Boolean(next.auto_strategy));
      const selected = datasets.find((item) => item.id === next.dataset_id);
      if (selected) setSymbol(cleanSymbolInput(selected.symbol));
      toast.success('虚拟账户已创建，可以开始逐根推送行情。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '账户创建失败。'); }
    finally { setCreating(false); }
  };

  const toggleAutoStrategy = async () => {
    if (!account) return;
    setTogglingAuto(true); setError('');
    try {
      const next = await api.setAutoStrategy(account.id, !account.auto_strategy);
      setAccount(next);
      setAutoStrategy(Boolean(next.auto_strategy));
      toast.success(next.auto_strategy ? '自动策略已开启：将复用回测策略状态机。' : '自动策略已关闭：仅保留手工委托撮合。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '自动策略切换失败。');
    } finally {
      setTogglingAuto(false);
    }
  };

  // 做T净利止盈 + T+1截止时间：改完立刻 PATCH 账户 config，下一根K线起的新操作即用新值。
  const [tSpreadInput, setTSpreadInput] = useState('');
  const [savingTSpread, setSavingTSpread] = useState(false);
  const tNetSpread = account?.config?.t_target_net_spread ?? defaultConfig.t_target_net_spread ?? 0.03;
  const [tCutoffInput, setTCutoffInput] = useState('');
  const [savingTCutoff, setSavingTCutoff] = useState(false);
  const tCutoff = account?.config?.t_exit_cutoff ?? defaultConfig.t_exit_cutoff ?? '14:50';
  // 超期降阈：持有 ≥N 交易日后止盈降到 override（两项需同时配置才生效）。
  const [holdingDaysInput, setHoldingDaysInput] = useState('');
  const [holdingTpInput, setHoldingTpInput] = useState('');
  const [savingHolding, setSavingHolding] = useState(false);
  const holdingDays = account?.config?.max_holding_days ?? null;
  const holdingTp = account?.config?.holding_tp_override ?? null;
  // 指数 beta 腿（ETF）：个股空仓时用创业板 ETF 承接 beta，选到个股后卖出换股。
  const [etfSymbolInput, setEtfSymbolInput] = useState('');
  const [etfPctInput, setEtfPctInput] = useState('');
  const [etfEntryMaInput, setEtfEntryMaInput] = useState('');
  const [etfExitMaInput, setEtfExitMaInput] = useState('');
  const [etfStopInput, setEtfStopInput] = useState('');
  const [etfDevInput, setEtfDevInput] = useState('');
  const [etfCooldownInput, setEtfCooldownInput] = useState('');
  const [savingEtf, setSavingEtf] = useState(false);
  const etfEnabled = Boolean(account?.config?.etf_beta_enabled);
  const etfSymbol = account?.config?.etf_symbol ?? 'sz.159915';
  const etfPct = account?.config?.etf_position_pct ?? 0.2;
  const etfEntryMa = account?.config?.etf_entry_ma_days ?? 60;
  const etfExitMa = account?.config?.etf_exit_ma_days ?? 20;
  const etfStop = account?.config?.etf_hard_stop_pct ?? 0.06;
  const etfDev = account?.config?.etf_entry_max_deviation ?? 0;
  const etfCooldown = account?.config?.etf_reentry_cooldown_days ?? 0;
  // 加仓按档位「先松后紧」：A3 浮亏信号严格度随 16 笔序列档位逐级叠加（档4 = 原始 A3）。
  const [savingTier, setSavingTier] = useState(false);
  const tierStrict = account?.config?.add_tier_strict ?? true;
  const toggleTierStrict = async (checked: boolean) => {
    if (!account) return;
    setSavingTier(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, { add_tier_strict: checked });
      setAccount(next);
      toast.success(checked ? '加仓档位分级已开启：A3 由松到紧逐档加严。' : '加仓档位分级已关闭：全档统一原始 A3。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加仓档位分级更新失败。');
    } finally {
      setSavingTier(false);
    }
  };
  // 深跌幅当日做T：日内跌幅 ≥ 阈值时绕过底仓笔数门槛（关闭传 0，后端归一化为 None）。
  const [deepDropInput, setDeepDropInput] = useState('');
  const [savingDeepDrop, setSavingDeepDrop] = useState(false);
  const deepDropPct = account?.config?.t_deep_drop_pct ?? null;
  const saveDeepDrop = async (enabled: boolean) => {
    if (!account) return;
    setSavingDeepDrop(true); setError('');
    try {
      if (!enabled) {
        const next = await api.updateAccountConfig(account.id, { t_deep_drop_pct: 0 });
        setAccount(next); setDeepDropInput('');
        toast.success('深跌幅当日做T已关闭。');
      } else {
        const pct = Number(deepDropInput);
        if (!Number.isFinite(pct) || pct < 10 || pct > 20) {
          setError('深跌幅阈值需为 10–20 之间的百分数（20cm 板极端恐慌线）。');
          return;
        }
        const next = await api.updateAccountConfig(account.id, { t_deep_drop_pct: pct / 100 });
        setAccount(next); setDeepDropInput('');
        toast.success(`深跌幅当日做T已开启：日内跌幅 ≥ ${number(pct, 1)}% 时绕过底仓笔数门槛。`);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '深跌幅当日做T更新失败。');
    } finally {
      setSavingDeepDrop(false);
    }
  };

  const saveTNetSpread = async () => {
    if (!account) return;
    const pct = Number(tSpreadInput);
    if (!Number.isFinite(pct) || pct <= 0 || pct >= 100) { setError('做T净利止盈需为 0–100 之间的百分数。'); return; }
    setSavingTSpread(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, { t_target_net_spread: pct / 100 });
      setAccount(next);
      setTSpreadInput('');
      toast.success(`做T净利止盈已更新为 ${number(pct, 2)}%，下一根K线起生效。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '做T净利止盈更新失败。');
    } finally {
      setSavingTSpread(false);
    }
  };

  const saveTCutoff = async () => {
    if (!account) return;
    const value = tCutoffInput.trim();
    if (!/^\d{1,2}:\d{2}$/.test(value)) { setError('T+1截止时间需为 HH:MM 格式，如 14:50。'); return; }
    const [hh, mm] = value.split(':').map(Number);
    if (hh < 0 || hh > 23 || mm < 0 || mm > 59) { setError('T+1截止时间需为合法的 24 小时制时间。'); return; }
    const normalized = `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
    setSavingTCutoff(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, { t_exit_cutoff: normalized });
      setAccount(next);
      setTCutoffInput('');
      toast.success(`做T的T+1尾盘截止时间已更新为 ${normalized}，下一根K线起生效。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '做T截止时间更新失败。');
    } finally {
      setSavingTCutoff(false);
    }
  };

  const saveHoldingOverride = async () => {
    if (!account) return;
    const days = Number(holdingDaysInput);
    const pct = Number(holdingTpInput);
    if (!Number.isInteger(days) || days < 1) { setError('超期降阈天数需为正整数交易日。'); return; }
    if (!Number.isFinite(pct) || pct <= 0 || pct >= 100) { setError('降阈后止盈需为 0–100 之间的百分数。'); return; }
    setSavingHolding(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, {
        max_holding_days: days, holding_tp_override: pct / 100,
      });
      setAccount(next);
      setHoldingDaysInput('');
      setHoldingTpInput('');
      toast.success(`超期降阈已更新：持有 ≥${days} 个交易日后止盈降为 ${number(pct, 2)}%，下一根K线起生效。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '超期降阈更新失败。');
    } finally {
      setSavingHolding(false);
    }
  };

  const toggleEtfBeta = async (enabled: boolean) => {
    if (!account) return;
    setSavingEtf(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, { etf_beta_enabled: enabled });
      setAccount(next);
      toast.success(enabled
        ? `ETF beta 腿已开启（${etfSymbol}，${number(etfPct * 100, 0)}% 权益）；个股空仓且指数在 ${etfEntryMa} 日线上方时持有，选到个股后卖出换股。`
        : 'ETF beta 腿已关闭，已有 ETF 持仓不会被自动清仓。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'ETF beta 腿切换失败。');
    } finally {
      setSavingEtf(false);
    }
  };

  const saveEtfConfig = async () => {
    if (!account) return;
    const patch: Record<string, unknown> = {};
    if (etfSymbolInput) patch.etf_symbol = etfSymbolInput;
    if (etfPctInput !== '') {
      const pct = Number(etfPctInput);
      if (!Number.isFinite(pct) || pct <= 0 || pct > 100) { setError('ETF 仓位需为 0–100 之间的百分数。'); return; }
      patch.etf_position_pct = pct / 100;
    }
    if (etfEntryMaInput !== '') {
      const value = Number(etfEntryMaInput);
      if (!Number.isInteger(value) || value < 2 || value > 250) { setError('入场均线需为 2–250 的整数交易日。'); return; }
      patch.etf_entry_ma_days = value;
    }
    if (etfExitMaInput !== '') {
      const value = Number(etfExitMaInput);
      if (!Number.isInteger(value) || value < 2 || value > 250) { setError('出场均线需为 2–250 的整数交易日。'); return; }
      patch.etf_exit_ma_days = value;
    }
    if (etfStopInput !== '') {
      const value = Number(etfStopInput);
      if (!Number.isFinite(value) || value <= 0 || value >= 100) { setError('ETF 硬止损需为 0–100 之间的百分数。'); return; }
      patch.etf_hard_stop_pct = value / 100;
    }
    if (etfDevInput !== '') {
      const value = Number(etfDevInput);
      if (!Number.isFinite(value) || value < 0 || value >= 100) { setError('不追高上限需为 0–100 之间的百分数（0 关闭）。'); return; }
      patch.etf_entry_max_deviation = value / 100;
    }
    if (etfCooldownInput !== '') {
      const value = Number(etfCooldownInput);
      if (!Number.isInteger(value) || value < 0 || value > 60) { setError('清仓后冷却需为 0–60 的整数交易日（0 关闭）。'); return; }
      patch.etf_reentry_cooldown_days = value;
    }
    if (!Object.keys(patch).length) { setError('没有要更新的 ETF 配置字段。'); return; }
    setSavingEtf(true); setError('');
    try {
      const next = await api.updateAccountConfig(account.id, patch);
      setAccount(next);
      setEtfSymbolInput(''); setEtfPctInput(''); setEtfEntryMaInput('');
      setEtfExitMaInput(''); setEtfStopInput('');
      setEtfDevInput(''); setEtfCooldownInput('');
      toast.success('ETF beta 腿配置已更新，下一根 K 线起生效。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'ETF 配置更新失败。');
    } finally {
      setSavingEtf(false);
    }
  };

  const toggleRotation = async () => {
    if (!account) return;
    setTogglingRotation(true);
    setError('');
    try {
      const next = await api.setRotation(account.id, { enabled: !account.rotation?.enabled });
      setAccount(next);
      toast.success(next.rotation?.enabled ? '池内轮动已开启：清仓后按 L4 顺序换票，L0 关门则空仓等待。' : '池内轮动已关闭。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '轮动开关切换失败。');
    } finally {
      setTogglingRotation(false);
    }
  };

  const placeOrder = async () => {
    if (!account) return;
    const code = cleanSymbolInput(symbol);
    if (code.length !== 6) { setError('请输入 6 位 A 股代码，例如 688200；沪深交易所由后端自动识别。'); return; }
    if (!Number.isInteger(quantity) || quantity <= 0) { setError('请填写正整数委托数量。'); return; }
    if (orderType === 'limit' && (!Number(limitPrice) || Number(limitPrice) <= 0)) { setError('限价委托需要填写大于 0 的委托价格。'); return; }
    setSubmitting(true); setError('');
    try {
      const result = await api.placeOrder(account.id, {
        symbol: code, side, order_type: orderType, quantity,
        limit_price: orderType === 'limit' ? Number(limitPrice) : undefined,
        purpose,
      });
      setAccount(result.account);
      toast.success(`${sideLabel(side)}委托已提交，状态：${statusLabel(result.order.status)}。`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '委托提交失败。'); }
    finally { setSubmitting(false); }
  };

  const cancelOrder = async (id: string | number) => {
    if (!account) return;
    setCancelling(id); setError('');
    try { setAccount(await api.cancelOrder(account.id, id)); toast.success('委托已撤销，账户状态已同步。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '撤单失败。'); }
    finally { setCancelling(null); }
  };

  const resetAccount = async () => {
    if (!account) return;
    setResetting(true); setError(''); setPlaying(false);
    try {
      setAccount(await api.resetAccount(account.id));
      toast.success('虚拟账户已恢复至初始状态。');
      setPendingReset(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '账户重置失败。'); }
    finally { setResetting(false); }
  };

  const currentDataset = datasets.find((item) => item.id === account?.dataset_id);
  const canCancel = (status: string) => ['open', 'pending', 'partial', 'partially_filled', 'new'].includes(status.toLowerCase());

  return (
    <div className="animate-[page-in_.22s_ease-out]">
      <PageHeader
        eyebrow="EXECUTION / PAPER"
        title="模拟交易"
        description="接入腾讯 5 分钟实时快照，观察委托、T+1 可卖约束与双账本状态。"
        actions={account ? <Button variant="outline" size="sm" onClick={() => void refreshAccount(account.id)} disabled={loading}><RefreshCw className={loading ? 'animate-spin' : ''} />刷新账户</Button> : undefined}
      />
      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}

      {loading && !account ? <Panel><Loading label="正在读取模拟账户" /></Panel> : !account ? (
        <Panel title="建立本地虚拟账户" description="账户仅用于规则与撮合验证，不连接券商，不产生真实资金变动。" action={<Landmark className="size-4" />}>
          <FormGrid>
            <Field label="账户名称" wide><Input value={accountName} onChange={(event) => setAccountName(event.target.value)} /></Field>
            <Field label="初始资金"><NumberInput unit="CNY" min={1} step={10000} precision={0} value={initialCash} onChange={(value) => { const parsed = Number(value); setInitialCash(Number.isFinite(parsed) ? parsed : 0); }} /></Field>
            <Field label="行情数据集" hint="用于逐根推送" wide>
              <SelectField value={datasetId || '__none__'} onValueChange={(value) => { const nextValue = value === '__none__' ? '' : value; setDatasetId(nextValue); const selected = datasets.find((item) => item.id === nextValue); if (selected) setSymbol(cleanSymbolInput(selected.symbol)); }} options={[{ value: '__none__', label: '暂不绑定' }, ...datasets.map((item) => ({ value: item.id, label: `${item.name} · ${displaySymbol(item.symbol)} · ${item.timeframe}` }))]} ariaLabel="行情数据集" />
            </Field>
            <SwitchRow label="自动执行策略" description="复用回测 LayeredTStrategy 状态机" checked={autoStrategy} onCheckedChange={setAutoStrategy} className="w-full self-end sm:w-[260px]" />
          </FormGrid>
          <Button className="mt-3 w-full sm:w-auto" onClick={() => void createAccount()} disabled={creating}>{creating ? <><Loader2 className="animate-spin" />正在创建</> : <><Plus />创建虚拟账户</>}</Button>
        </Panel>
      ) : (
        <>
          <div className="mb-3 flex flex-col gap-2 rounded-xl border bg-card px-3.5 py-3 shadow-xs lg:flex-row lg:items-center lg:justify-between">
            <div className="flex items-center gap-2.5">
              <span className="grid size-9 place-items-center rounded-lg bg-accent font-mono text-sm font-medium text-accent-foreground">{account.name.slice(0, 1).toUpperCase()}</span>
              <div><strong className="block text-[13px]">{account.name}</strong><span className="block font-mono text-[10px] text-muted-foreground">ID {account.id.slice(0, 12)} · 游标 {integer(account.cursor)}</span></div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => void toggleAutoStrategy()} disabled={togglingAuto}><RefreshCw className={togglingAuto ? 'animate-spin' : ''} />{account.auto_strategy ? '自动策略开启' : '自动策略关闭'}</Button>
              {currentDataset?.is_synthetic && <SyntheticNotice compact />}
              <Badge tone={lower(account.status) === 'active' ? 'green' : 'neutral'}>{statusLabel(account.status)}</Badge>
              {rotation?.enabled && <Badge tone="indigo">池内轮动</Badge>}
              <span className="text-[11px] text-muted-foreground">状态机 <b className="font-mono text-foreground">{account.armed_state}</b></span>
              <span className="text-[11px] text-muted-foreground">序列 <b className="font-mono text-foreground">{account.sequence_index} / 16</b></span>
            </div>
          </div>

          <MetricGrid columns={4}>
            <Metric label="账户权益" value={money(account.equity)} icon={<Wallet className="size-3.5" />} sub={`初始资金 ${money(account.initial_cash, 0)}`} />
            <Metric label="可用资金" value={money(account.available_cash)} sub={`账面现金 ${money(account.cash)}`} />
            <Metric label="持仓市值" value={money(account.market_value)} sub={`敞口 ${account.equity ? number(account.market_value / account.equity * 100, 1) : '0.0'}%`} />
            <Metric label="已实现盈亏" value={signedMoney(account.realized_pnl)} tone={account.realized_pnl >= 0 ? 'positive' : 'negative'} sub={`做 T 净贡献 ${signedMoney(account.t_net_profit)}`} />
          </MetricGrid>

          {account.auto_strategy && (
            <Panel className="mb-3" title="做 T 止盈与截止" description="T 仓扣完成本后净利达到该幅度即平 T（T0 当天满足就当天做完）；到 T+1 尾盘截止时间仍未止盈则无条件强平。改动保存后下一根 K 线起生效。" action={<Badge tone="indigo">即时生效</Badge>}>
              <div className="grid gap-2 lg:grid-cols-2">
                <div className="flex items-start gap-2">
                  <Field label="净利止盈（%）" hint={`当前 ${number(tNetSpread * 100, 2)}%（扣成本后）`} className="flex-1"><NumberInput unit="%" min={0} max={100} step={0.1} allowEmpty placeholder={number(tNetSpread * 100, 2)} value={tSpreadInput} onChange={setTSpreadInput} disabled={savingTSpread} /></Field>
                  <Button size="sm" className="mt-[24px] h-8" onClick={() => void saveTNetSpread()} disabled={savingTSpread || tSpreadInput === ''}>{savingTSpread ? <Loader2 className="animate-spin" /> : <Repeat2 />}{savingTSpread ? '正在保存' : '保存止盈'}</Button>
                </div>
                <div className="flex items-start gap-2">
                  <Field label="T+1 截止时间" hint={`当前 ${tCutoff}（HH:MM，尾盘强平）`} className="flex-1"><Input type="time" step={300} value={tCutoffInput} onChange={(event) => setTCutoffInput(event.target.value)} disabled={savingTCutoff} /></Field>
                  <Button size="sm" className="mt-[24px] h-8" onClick={() => void saveTCutoff()} disabled={savingTCutoff || tCutoffInput === ''}>{savingTCutoff ? <Loader2 className="animate-spin" /> : <Repeat2 />}{savingTCutoff ? '正在保存' : '保存截止'}</Button>
                </div>
              </div>
              <div className="mt-1 flex flex-wrap items-end gap-2 border-t pt-2">
                <div className="flex items-center gap-2 py-1.5">
                  <Switch checked={deepDropPct != null} onCheckedChange={(checked) => (checked ? setDeepDropInput(String(Number(((deepDropPct ?? 0.1) * 100).toFixed(1)))) : void saveDeepDrop(false))} disabled={savingDeepDrop} />
                  <div>
                    <strong className="block text-[12px]">深跌幅当日做T</strong>
                    <span className="text-[10px] text-muted-foreground">{deepDropPct != null ? `日内跌幅 ≥ ${number(deepDropPct * 100, 1)}% 时绕过底仓笔数门槛（极端恐慌日抢反弹）` : '关闭'}</span>
                  </div>
                </div>
                {deepDropPct != null && (
                  <>
                    <Field label="阈值（%）" hint="10–20，20cm 板极端恐慌线" className="w-40"><NumberInput unit="%" min={10} max={20} step={0.5} allowEmpty placeholder={number(deepDropPct * 100, 1)} value={deepDropInput} onChange={setDeepDropInput} disabled={savingDeepDrop} /></Field>
                    <Button size="sm" className="h-8" onClick={() => void saveDeepDrop(true)} disabled={savingDeepDrop || deepDropInput === ''}>{savingDeepDrop ? <Loader2 className="animate-spin" /> : <Repeat2 />}{savingDeepDrop ? '正在保存' : '保存阈值'}</Button>
                  </>
                )}
              </div>
              <p className="mt-2 text-[10px] leading-4 text-muted-foreground">留空表示沿用当前值（止盈 {number(tNetSpread * 100, 2)}%、截止 {tCutoff}）；填入新值后点保存。做 T 为正 T，卖出走可卖底仓份额、不裸卖。</p>
            </Panel>
          )}

          {account.auto_strategy && (
            <Panel className="mb-3" title="加仓按档位 · 先松后紧" description="A3 浮亏加仓信号随 16 笔序列档位逐级加严：早期小仓易加让资金打满，后段高门槛封顶暴露。浮盈右侧回撤 5% 的加仓在所有档位保持不变；关闭则全档统一原始 A3。" action={<div className="flex items-center gap-2"><span className="text-[10px] text-muted-foreground">{tierStrict ? '逐档加严' : '全档统一'}</span><Switch checked={tierStrict} onCheckedChange={(checked) => void toggleTierStrict(checked)} disabled={savingTier} /></div>}>
              <div className="grid gap-1.5 text-[11px] sm:grid-cols-2">
                <div className="rounded-lg border bg-muted/20 px-2.5 py-1.5"><strong className="text-foreground">档1 · 10万</strong> · 最松：浮亏 + 当日缩量（量 &lt; 5日均量）</div>
                <div className="rounded-lg border bg-muted/20 px-2.5 py-1.5"><strong className="text-foreground">档2 · 20万</strong> · + 收盘站回前3日低点</div>
                <div className="rounded-lg border bg-muted/20 px-2.5 py-1.5"><strong className="text-foreground">档3 · 30万</strong> · + 此前破过新低</div>
                <div className="rounded-lg border bg-muted/20 px-2.5 py-1.5"><strong className="text-foreground">档4 · 40万</strong> · 最严：+ 缩量 &lt; 5日均量 × 0.5（原始 A3）</div>
              </div>
              <p className="mt-2 text-[10px] leading-4 text-muted-foreground">开关保存后下一根 K 线起生效；仅影响浮亏 A3 分支。对应配置字段 add_tier_strict（回测页同步可调，可先回测验证）。</p>
            </Panel>
          )}

          {account.auto_strategy && (
            <Panel className="mb-3" title="超期降阈止盈" description={`持有交易日数达到阈值后，价格轨/账户轨止盈阈值从 15% 降为设定值。当前：${holdingDays != null && holdingTp != null ? `持有 ≥${holdingDays} 日 → ${number(holdingTp * 100, 2)}%` : '关闭'}。两项需同时填写，保存后下一根 K 线起生效。`} action={<Badge tone={holdingDays != null && holdingTp != null ? 'indigo' : 'neutral'}>{holdingDays != null && holdingTp != null ? '已开启' : '关闭'}</Badge>}>
              <div className="grid items-start gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
                <Field label="超期天数（交易日）" hint="持有 ≥N 个交易日后降阈"><NumberInput unit="日" min={1} max={250} step={1} allowEmpty placeholder={holdingDays != null ? String(holdingDays) : '22'} value={holdingDaysInput} onChange={setHoldingDaysInput} disabled={savingHolding} /></Field>
                <Field label="降阈后止盈（%）" hint="价格轨/账户轨同时降为该值"><NumberInput unit="%" min={0} max={100} step={0.1} allowEmpty placeholder={holdingTp != null ? number(holdingTp * 100, 2) : '3'} value={holdingTpInput} onChange={setHoldingTpInput} disabled={savingHolding} /></Field>
                <Button size="sm" className="h-8 justify-self-start sm:mt-[24px]" onClick={() => void saveHoldingOverride()} disabled={savingHolding || holdingDaysInput === '' || holdingTpInput === ''}>{savingHolding ? <Loader2 className="animate-spin" /> : <Repeat2 />}{savingHolding ? '正在保存' : '保存降阈'}</Button>
              </div>
              <p className="mt-2 text-[10px] leading-4 text-muted-foreground">两项需同时填写才生效；只填一项时后端维持原 15% 止盈。建议 22 个交易日 / 3%。</p>
            </Panel>
          )}

          {account.auto_strategy && (
            <Panel className="mb-3" title="指数 beta 腿（创业板 ETF）" description={`个股空仓期间用 ${etfSymbol} 承接 beta：指数收盘站上 ${etfEntryMa} 日线买入，跌破 ${etfExitMa} 日线或 ETF 较成本回撤 ${number(etfStop * 100, 1)}% 卖出；一旦选出个股并建仓，ETF 卖出换股。`} action={<div className="flex items-center gap-2"><span className="text-[10px] text-muted-foreground">{etfEnabled ? '空仓期持有 ETF' : '空仓期纯现金'}</span><Switch checked={etfEnabled} onCheckedChange={(checked) => void toggleEtfBeta(checked)} disabled={savingEtf} /></div>}>
              <FormGrid>
                <Field label="ETF 代码" hint={`当前 ${etfSymbol}`}><SelectField value={etfSymbolInput || etfSymbol} onValueChange={setEtfSymbolInput} disabled={savingEtf || !etfEnabled} options={[{ value: 'sz.159915', label: 'sz.159915 · 创业板ETF易方达' }, { value: 'sz.159952', label: 'sz.159952 · 创业板ETF广发' }, { value: 'sz.159964', label: 'sz.159964 · 创业板ETF平安' }]} ariaLabel="ETF 代码" /></Field>
                <Field label="仓位（%权益）" hint={`当前 ${number(etfPct * 100, 0)}%`}><NumberInput unit="%" min={1} max={100} step={1} allowEmpty placeholder={number(etfPct * 100, 0)} value={etfPctInput} onChange={setEtfPctInput} disabled={savingEtf || !etfEnabled} /></Field>
                <Field label="入场均线（日）" hint={`当前 ${etfEntryMa} 日`}><NumberInput unit="日" min={2} max={250} step={1} allowEmpty placeholder={String(etfEntryMa)} value={etfEntryMaInput} onChange={setEtfEntryMaInput} disabled={savingEtf || !etfEnabled} /></Field>
                <Field label="出场均线（日）" hint={`当前 ${etfExitMa} 日`}><NumberInput unit="日" min={2} max={250} step={1} allowEmpty placeholder={String(etfExitMa)} value={etfExitMaInput} onChange={setEtfExitMaInput} disabled={savingEtf || !etfEnabled} /></Field>
                <Field label="硬止损（%）" hint={`当前 ${number(etfStop * 100, 1)}%`}><NumberInput unit="%" min={0.1} max={99} step={0.1} allowEmpty placeholder={number(etfStop * 100, 1)} value={etfStopInput} onChange={setEtfStopInput} disabled={savingEtf || !etfEnabled} /></Field>
                <Field label="不追高上限（偏离均线 %）" hint={`当前 ${etfDev > 0 ? `${number(etfDev * 100, 1)}%（关闭=0）` : '关闭'}`}><NumberInput unit="%" min={0} max={50} step={0.5} allowEmpty placeholder={number(etfDev * 100, 1)} value={etfDevInput} onChange={setEtfDevInput} disabled={savingEtf || !etfEnabled} /></Field>
                <Field label="清仓后冷却（交易日）" hint={`当前 ${etfCooldown > 0 ? `${etfCooldown} 个交易日（关闭=0）` : '关闭'}`}><NumberInput unit="日" min={0} max={60} step={1} allowEmpty placeholder={String(etfCooldown)} value={etfCooldownInput} onChange={setEtfCooldownInput} disabled={savingEtf || !etfEnabled} /></Field>
              </FormGrid>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Button size="sm" className="h-8" onClick={() => void saveEtfConfig()} disabled={savingEtf || !etfEnabled}>{savingEtf ? <Loader2 className="animate-spin" /> : <Repeat2 />}{savingEtf ? '正在保存' : '保存 ETF'}</Button>
                <p className="text-[10px] leading-4 text-muted-foreground">留空表示沿用当前值；ETF 不占 16 笔加仓序列，与个股互斥：个股建仓后 ETF 自动卖出。首版仅支持创业板 ETF（结算规则 100 份/手、T+1、±20%）。</p>
              </div>
            </Panel>
          )}

          <Panel className="mb-3" title="池内轮动 · 每次清仓后重新选股" description="清仓后自动重新运行五层选股；L0 大盘门关闭则空仓等待，不降格选票" action={<div className="flex items-center gap-2"><span className="text-[10px] text-muted-foreground">{rotation?.enabled ? '清仓后重新选股' : '开启后按新语义轮动'}</span><Switch checked={Boolean(rotation?.enabled)} onCheckedChange={() => void toggleRotation()} disabled={togglingRotation} /></div>}>
            {togglingRotation ? <Loading label="正在同步轮动状态" /> : !rotation || rotationCandidates.length === 0 ? (
              <EmptyState compact icon={<Repeat2 className="size-6" />} title="尚未开启轮动" description="在选股页对第一名点「带入模拟盘（开启轮动：每次清仓后重新选股）」，账户会在每次清仓后重新筛选并取当时第一名；已有账户可在此开启或关闭轮动。" />
            ) : (
              <>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  <div className="rounded-lg border bg-card px-3 py-2"><span className="block text-[10px] text-muted-foreground">当前标的</span><strong className="block font-mono text-[13px]">{rotation.current_symbol ? displaySymbol(rotation.current_symbol) : '空仓'}</strong><small className="block text-[10px] text-muted-foreground">{rotation.current_symbol || '等待清仓后重新选股'}</small></div>
                  <div className="rounded-lg border bg-card px-3 py-2"><span className="block text-[10px] text-muted-foreground">状态</span><strong className={cn('block text-[13px]', rotation.waiting && 'text-success')}>{rotation.waiting ? '空仓等待' : '正常推进'}</strong></div>
                  <div className="rounded-lg border bg-card px-3 py-2"><span className="block text-[10px] text-muted-foreground">最近重筛</span><strong className="block text-[13px]">{lastAttempt ? shortDate(lastAttempt) : '—'}</strong><small className="block text-[10px] text-muted-foreground">{lastScreen?.as_of ? `as_of ${shortDate(lastScreen.as_of)}` : '尚未触发过'}</small></div>
                  <div className="rounded-lg border bg-card px-3 py-2"><span className="block text-[10px] text-muted-foreground">L0 大盘门</span>{lastScreen?.gate_open === undefined ? <strong className="block text-[13px]">—</strong> : <Badge tone={lastScreen.gate_open ? 'green' : 'amber'}>{lastScreen.gate_open ? '开门' : '关门'}</Badge>}</div>
                </div>
                {rotation.note && <div className={cn('mt-2 flex items-start gap-1.5 rounded-lg border px-2.5 py-2 text-[11px]', rotation.waiting ? 'border-warning/25 bg-warning/5 text-warning' : 'bg-muted/40 text-muted-foreground')}><Repeat2 className="mt-0.5 size-3.5 shrink-0" /><span>{rotation.note}{rotation.waiting ? ' · 次日会自动重试' : ''}</span></div>}
                <div className="mt-3 rounded-lg border bg-muted/20 p-2.5">
                  <div className="mb-2 flex items-center justify-between gap-2"><strong className="text-[12px]">最近一次选股结果</strong><span className="font-mono text-[10px] text-muted-foreground">{lastRunId ? `run ${lastRunId}` : '暂无运行'}{typeof lastScreen?.eligible_count === 'number' ? ` · 符合条件 ${integer(lastScreen.eligible_count)} 只` : ''}</span></div>
                  <ol className="grid gap-1.5 md:grid-cols-2">
                    {(showAllPool ? rotationCandidates : rotationCandidates.slice(0, POOL_PREVIEW_LIMIT)).map((item, index) => {
                      const isFirst = index === 0 || item.symbol === lastScreenFirst?.code;
                      return (
                        <li key={item.symbol} className={cn('flex items-center gap-2 rounded-md border bg-card px-2.5 py-1.5', isFirst && 'border-primary/30 bg-accent/40')}>
                          <span className="font-mono text-[10px] text-muted-foreground">{String(index + 1).padStart(2, '0')}</span>
                          <div className="min-w-0 flex-1"><strong className="block font-mono text-[12px]">{displaySymbol(item.symbol)} <small className="font-sans font-normal text-muted-foreground">{item.name || item.symbol}</small></strong><span className="block text-[10px] text-muted-foreground">{item.board || '—'}{item.chg20d !== undefined && item.chg20d !== null ? ` · Chg20D ${signedPercent(item.chg20d)}` : ''}</span></div>
                          {isFirst ? <Badge tone="indigo">第 1 名</Badge> : <Badge>候选</Badge>}
                        </li>
                      );
                    })}
                  </ol>
                  {rotationCandidates.length > POOL_PREVIEW_LIMIT && <div className="mt-2 flex items-center justify-between text-[10px] text-muted-foreground"><span>共 {integer(rotationCandidates.length)} 只候选，默认仅展示前 {POOL_PREVIEW_LIMIT} 只</span><button className="text-primary hover:underline" onClick={() => setShowAllPool(!showAllPool)}>{showAllPool ? '收起列表' : `展开全部 ${integer(rotationCandidates.length)} 只`}</button></div>}
                  {lastScreen?.warnings && lastScreen.warnings.length > 0 && <div className="mt-2 flex items-start gap-1.5 rounded-md border border-warning/20 bg-warning/5 px-2 py-1 text-[10px] text-warning"><AlertTriangle className="mt-0.5 size-3 shrink-0" /><span>{lastScreen.warnings[0]}{lastScreen.warnings.length > 1 ? ` 等 ${lastScreen.warnings.length} 条警告` : ''}</span></div>}
                </div>
              </>
            )}
          </Panel>

          <div className="mb-2.5 grid gap-2.5 xl:grid-cols-[minmax(0,1fr)_minmax(0,0.9fr)]">
            <Panel title="腾讯实时行情" description={currentDataset ? `${currentDataset.name} · ${currentDataset.timeframe}` : '直接读取腾讯快照，无需绑定缓存数据集'} action={<Badge tone="indigo">5 MIN</Badge>}>
              <div className="grid gap-1.5">
                {quoteRows.length === 0 ? <EmptyState compact icon={<Activity className="size-6" />} title="等待实时快照" description="输入标的并拉取腾讯行情后，本地账户会显示最新价；不会用前端行情替代后端状态。" /> : quoteRows.map((quote) => {
                  const heldQty = heldQtyBySymbol.get(quote.symbol) ?? 0;
                  return (
                    <div className="flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2" key={quote.symbol}>
                      <div><strong className="block font-mono text-[12px]" title={quote.symbol}>{displaySymbol(quote.symbol)}</strong><span className="block text-[10px] text-muted-foreground">{heldQty > 0 ? `持仓 ${integer(heldQty)}` : '最新行情'}</span></div>
                      <div className="text-right"><strong className="block font-mono text-[13px]">{money(quote.last_price)}</strong><span className="block text-[10px] text-muted-foreground">后端最新价</span></div>
                    </div>
                  );
                })}
              </div>
              <div className="mt-2.5 grid gap-2.5">
                <div className="rounded-lg border bg-muted/25 p-2.5">
                  <div className="flex items-center justify-between gap-2"><strong className="inline-flex items-center gap-1 text-[12px]"><Radio className="size-3.5" />实时快照</strong><small className="text-[10px] text-muted-foreground">腾讯 · 5 分钟</small></div>
                  <div className="mt-2 flex items-center gap-2">
                    <Button variant="outline" size="sm" disabled={liveLoading || liveCode.length !== 6} onClick={() => void liveTick()}>{liveLoading ? <Loader2 className="animate-spin" /> : <Radio />}拉取实时快照</Button>
                    <Badge tone={autoSyncPaused ? 'neutral' : scheduler?.running ? 'green' : 'neutral'} title={autoSyncPaused ? '后端自动同步已暂停' : scheduler?.running ? '后端自动同步运行中' : '后端自动同步'}>后端自动</Badge>
                  </div>
                  <div className="mt-1.5 flex items-start gap-1.5 text-[10px] leading-4 text-muted-foreground"><Timer className="mt-0.5 size-3 shrink-0" /><span>{scheduler && !scheduler.enabled ? '后端定时器未启用，需手动拉取快照' : autoSyncPaused ? `${tradingSession && !tradingSession.is_trading_day ? '今日休市' : '已收盘'}，自动同步已暂停${lastSync ? `（最后同步 ${lastSync}）` : ''}${tradingSession?.next_trading_day ? `，${tradingSession.next_trading_day} 09:25 恢复` : '，下一交易日 09:25 恢复'}` : `成交由后端定时器每 5 分钟推进${lastSync ? `（最近 ${lastSync}）` : ''}，页面每 15 秒自动同步`}</span></div>
                </div>
                <div className="rounded-lg border bg-muted/25 p-2.5">
                  <div className="flex items-center justify-between gap-2"><strong className="inline-flex items-center gap-1 text-[12px]"><StepForward className="size-3.5" />缓存回放</strong><small className="text-[10px] text-muted-foreground">批量验证撮合与订单生命周期</small></div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    <Button variant="outline" size="sm" disabled={advancing || playing || !account.dataset_id} onClick={() => void advance(1)}><StepForward />推送 1 根</Button>
                    <Button variant="outline" size="sm" disabled={advancing || playing || !account.dataset_id} onClick={() => void advance(10)}><FastForward />推送 10 根</Button>
                    <Button variant={playing ? 'destructive' : 'outline'} size="sm" disabled={!account.dataset_id} onClick={() => setPlaying(!playing)}>{playing ? <Pause /> : <Play />}{playing ? '暂停回放' : '加速回放'}</Button>
                  </div>
                  <div className="mt-1.5 flex items-center gap-1.5 text-[10px] text-muted-foreground"><span className={cn('size-1.5 rounded-full', playing ? 'animate-pulse bg-primary' : 'bg-muted-foreground/50')} />{playing ? '回放模式：每 1.4 秒推进 1 根缓存 5 分钟K线' : advancing ? '正在推送行情并同步账户' : '回放已暂停 · 行情由后端逐根推进'}</div>
                </div>
              </div>
            </Panel>

            <Panel title="委托下单" description="订单将提交至本地仿真撮合引擎" action={<span className="font-mono text-[9px] tracking-wider text-muted-foreground">ORDER TICKET</span>}>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-[11px] text-muted-foreground"><span>仓位用途</span><Segmented value={purpose} onChange={(value) => setPurpose(value as OrderPurpose)} options={[{ value: 'PERMANENT', label: '永久仓' }, { value: 'T', label: '做T仓' }]} /></div>
                <div className="flex gap-1.5">
                  <button type="button" className={cn('flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px]', side === 'buy' ? 'border-destructive/30 bg-destructive/10 text-destructive' : 'bg-card text-muted-foreground')} onClick={() => setSide('buy')}><ArrowDownLeft className="size-3.5" />买入</button>
                  <button type="button" className={cn('flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px]', side === 'sell' ? 'border-success/30 bg-success/10 text-success' : 'bg-card text-muted-foreground')} onClick={() => setSide('sell')}><ArrowUpRight className="size-3.5" />卖出</button>
                </div>
              </div>
              <p className="mb-2 text-[10px] text-muted-foreground">{purpose === 'PERMANENT' ? '买入成交推进 16 笔序列；卖出为永久仓减仓。' : '不占 16 笔序列，次日可卖，盈亏单独统计。'}</p>
              <div className="mb-2 flex items-center gap-2 text-[11px]"><span className="text-muted-foreground">委托方式</span><Segmented value={orderType} onChange={(value) => { setOrderType(value as OrderType); resetTier(); }} options={[{ value: 'market', label: '市价' }, { value: 'limit', label: '限价' }]} /></div>
              <FormGrid>
                <Field label="标的代码" hint="6 位代码"><Input value={symbol} onChange={(event) => applySymbol(cleanSymbolInput(event.target.value))} placeholder="例如 688200" inputMode="numeric" maxLength={6} /></Field>
                <Field label="委托数量" hint={tradingRules ? `最小 ${integer(tradingRules.rules.min_qty)} 股` : '校验中…'}><NumberInput unit="股" min={1} step={tradingRules?.rules.step || 100} value={quantity} onChange={(value) => { setQuantity(Number(value)); resetTier(); }} /></Field>
                {orderType === 'limit' && <Field label="委托价格"><NumberInput unit="元" min={0.01} step={0.01} allowEmpty value={limitPrice} onChange={(value) => { setLimitPrice(value); resetTier(); }} placeholder="0.00" /></Field>}
              </FormGrid>
              <div className="mt-2 grid grid-cols-4 gap-1.5">
                {AMOUNT_TIERS.map((amount) => (
                  <Button key={amount} type="button" variant="outline" size="sm" className={cn('font-mono', tierAmount === amount && 'border-primary/40 bg-accent text-accent-foreground')} disabled={tierPicking !== null} title="按参考价与板块规则换算股数" onClick={() => void applyAmountTier(amount)}>
                    {tierPicking === amount ? <Loader2 className="animate-spin" /> : `${amount / 10_000}万`}
                  </Button>
                ))}
              </div>
              {tierNote && <div className={cn('mt-1.5 rounded-md px-2 py-1 text-[10px]', tierError ? 'bg-destructive/5 text-destructive' : 'bg-muted/50 text-muted-foreground')}>{tierNote}</div>}
              {qtyError && <div className="mt-1.5 flex items-start gap-1.5 rounded-md border border-warning/25 bg-warning/5 px-2 py-1 text-[10px] text-warning"><AlertTriangle className="mt-0.5 size-3 shrink-0" /><span>{qtyError}</span></div>}
              <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-muted-foreground"><span>可用资金 <b className="font-mono text-foreground">{money(account.available_cash)}</b></span>{orderType === 'limit' && limitPrice && <span>委托金额约 <b className="font-mono text-foreground">{money(Number(limitPrice) * quantity)}</b></span>}</div>
              <Button className={cn('mt-2 w-full', side === 'buy' ? 'bg-destructive text-white hover:bg-destructive/90' : 'bg-success text-white hover:bg-success/90')} disabled={submitting || Boolean(qtyError)} onClick={() => void placeOrder()}>{submitting ? <Loader2 className="animate-spin" /> : <Send />}{submitting ? '正在提交委托' : `提交${sideLabel(side)}委托`}</Button>
              <p className="mt-1.5 text-center text-[9px] text-muted-foreground">虚拟委托，不会发送至任何真实交易市场。</p>
            </Panel>
          </div>

          <Panel className="mb-3" title="持仓与双账本" description="永久仓和 T 仓分账记录，可卖数量遵循 T+1 约束；平均成本已含买入手续费" action={<span className="text-[10px] text-muted-foreground" title="佣金 + 印花税 + 经手费 + 证管费 + 过户费；滑点体现在成交价，不计入费用">累计费用 <b className="font-mono text-foreground">{money(account.total_fees)}</b>（五项合计）</span>} flush>
            <Table>
              <TableHeader><TableRow><TableHead>标的</TableHead><TableHead className="text-right">最新价</TableHead><TableHead className="text-right">总持仓</TableHead><TableHead className="text-right">可卖数量</TableHead><TableHead className="text-right">永久仓</TableHead><TableHead className="text-right">永久仓成本</TableHead><TableHead className="text-right" title="平均成本 = 总成本 / 总持仓，已包含买入手续费">平均成本（含费）</TableHead><TableHead className="text-right" title="当前持仓成本中仍包含的买入手续费；卖出费用计入已实现盈亏">买入费用</TableHead><TableHead className="text-right">T 仓在途</TableHead><TableHead className="text-right">持仓市值</TableHead><TableHead className="text-right">浮动盈亏</TableHead></TableRow></TableHeader>
              <TableBody>
                {holdingPositions.length === 0 ? <TableEmpty colSpan={11} message="暂无持仓，提交买入委托并推送行情后查看" /> : holdingPositions.map((position) => {
                  const floatPnl = position.float_pnl;
                  return <TableRow key={position.symbol}><TableCell><strong className="font-mono text-[12px]" title={position.symbol}>{displaySymbol(position.symbol)}</strong></TableCell><TableCell className="text-right font-mono text-[12px]">{money(position.last_price)}</TableCell><TableCell className="text-right font-mono text-[12px]"><strong>{integer(position.total_qty)}</strong></TableCell><TableCell className="text-right font-mono text-[12px]">{integer(position.sellable_qty)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(position.permanent_qty)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(position.permanent_cost)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(position.average_cost)}</TableCell><TableCell className="text-right font-mono text-[12px]" title="已包含在平均成本内">{money(position.buy_fee ?? 0)}</TableCell><TableCell className="text-right font-mono text-[12px]">{position.t_active_qty > 0 ? <Badge tone="indigo">{integer(position.t_active_qty)}</Badge> : '0'}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(position.market_value)}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', floatPnl >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(floatPnl)}</TableCell></TableRow>;
                })}
              </TableBody>
            </Table>
          </Panel>

          <Panel className="mb-3" flush>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2.5">
              <Tabs value={recordTab} onValueChange={(value) => setRecordTab(value as 'orders' | 'trades')}>
                <TabsList>
                  <TabsTrigger value="orders">订单<span className="ml-1 font-mono text-[10px] text-muted-foreground">{account.orders.length}</span></TabsTrigger>
                  <TabsTrigger value="trades">成交<span className="ml-1 font-mono text-[10px] text-muted-foreground">{account.trades.length}</span></TabsTrigger>
                </TabsList>
              </Tabs>
              <Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10" onClick={() => setPendingReset(true)} disabled={resetting}><RotateCcw className={resetting ? 'animate-spin' : ''} />{resetting ? '正在重置' : '重置虚拟账户'}</Button>
            </div>
            {recordTab === 'orders' ? <Table><TableHeader><TableRow><TableHead>委托时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>类型</TableHead><TableHead className="text-right">限价</TableHead><TableHead className="text-right">委托数量</TableHead><TableHead className="text-right">已成交</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>
              {account.orders.length === 0 ? <TableEmpty colSpan={9} message="暂无订单记录" /> : account.orders.map((order) => <TableRow key={order.id}><TableCell>{dateTime(order.created_at || order.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={order.symbol}>{displaySymbol(order.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(order.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(order.side)}</span></TableCell><TableCell>{orderTypeLabel(order.order_type || order.type)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(order.limit_price ?? order.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.filled_quantity ?? order.filled_qty)}</TableCell><TableCell><Badge tone={lower(order.status) === 'filled' ? 'green' : lower(order.status) === 'rejected' ? 'red' : 'neutral'}>{statusLabel(order.status)}</Badge></TableCell><TableCell>{canCancel(order.status) ? <button className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-destructive hover:bg-destructive/10" disabled={cancelling === order.id} onClick={() => void cancelOrder(order.id)}>{cancelling === order.id ? <RefreshCw className="size-3 animate-spin" /> : <XCircle className="size-3" />}撤单</button> : <span className="text-muted-foreground">—</span>}</TableCell></TableRow>)}
            </TableBody></Table> : <Table><TableHeader><TableRow><TableHead>成交时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>用途</TableHead><TableHead className="text-right">价格</TableHead><TableHead className="text-right">数量</TableHead><TableHead className="text-right">成交额</TableHead><TableHead className="text-right">费用</TableHead><TableHead className="text-right">已实现盈亏</TableHead></TableRow></TableHeader><TableBody>
              {account.trades.length === 0 ? <TableEmpty colSpan={9} message="暂无成交记录" /> : account.trades.map((trade, index) => <TableRow key={String(trade.id ?? index)}><TableCell>{dateTime(trade.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={trade.symbol}>{displaySymbol(trade.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(trade.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(trade.side)}</span></TableCell><TableCell>{purposeLabel(trade.purpose)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(trade.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.amount ?? trade.notional)}</TableCell><TableCell className="text-right font-mono text-[12px]" title={feeBreakdownTitle(trade)}>{money(trade.total_fee ?? trade.fee)}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', (trade.realized_pnl || 0) >= 0 ? 'text-destructive' : 'text-success')}>{trade.realized_pnl === undefined ? '—' : signedMoney(trade.realized_pnl)}</TableCell></TableRow>)}
            </TableBody></Table>}
          </Panel>

          <div className="mb-3 flex items-start gap-1.5 text-[10px] leading-4 text-muted-foreground"><CircleDollarSign className="mt-0.5 size-3.5 shrink-0" /><span>账户权益与盈亏均由本地后端仿真状态计算。模拟结果不代表实盘表现，也不构成收益承诺。</span></div>

          <Panel className="mb-3" title={`交易规则 · ${rulesView === 'all' ? '全部板块对比' : (tradingRules?.board || '按标的加载')}`} description={rulesView === 'all' ? '六板块口径并列对比；下单校验仍以当前标的所属板块为准' : (tradingRules ? (tradingRules.is_modeled_board ? tradingRules.rules.rule_basis : '合成标的无板块，回退「100股整数手 + ±20%」旧口径') : '输入 6 位标的代码后加载其板块交易规则')} action={<div className="flex items-center gap-2"><Segmented value={rulesView} onChange={(value) => setRulesView(value as 'current' | 'all')} options={[{ value: 'current', label: '当前标的' }, { value: 'all', label: '全部板块' }]} /><Button variant="outline" size="sm" onClick={() => setRulesOpen(!rulesOpen)}>{rulesOpen ? '收起' : '展开'}</Button></div>}>
            {rulesView === 'all' ? (
              allBoards.length === 0 ? <Loading label="正在加载六板块规则全表" /> : (
                <div className="grid gap-2">
                  <Table>
                    <TableHeader><TableRow><TableHead>板块</TableHead><TableHead className="text-right">最小申报</TableHead><TableHead className="text-right">递增</TableHead><TableHead className="text-right">限价上限</TableHead><TableHead className="text-right">市价上限</TableHead><TableHead className="text-right">零股卖出</TableHead><TableHead className="text-right">涨跌幅</TableHead><TableHead className="text-right">价格笼子</TableHead></TableRow></TableHeader>
                    <TableBody>
                      {allBoards.map((board) => {
                        const isCurrent = tradingRules?.board === board.board;
                        return <TableRow key={board.board} className={isCurrent ? 'bg-accent/40' : ''}><TableCell><strong className="inline-flex items-center gap-1.5">{board.board}{isCurrent && <Badge tone="indigo">当前</Badge>}</strong></TableCell><TableCell className="text-right font-mono text-[12px]">{integer(board.min_qty)} 股</TableCell><TableCell className="text-right font-mono text-[12px]">{board.step === 1 ? '1 股' : `${integer(board.step)} 整数倍`}</TableCell><TableCell className="text-right font-mono text-[12px]">{board.limit_order_cap > 0 ? `${integer(board.limit_order_cap)} 股` : '未建模'}</TableCell><TableCell className="text-right font-mono text-[12px]">{board.market_order_cap > 0 ? `${integer(board.market_order_cap)} 股` : '未建模'}</TableCell><TableCell className="text-right font-mono text-[12px]">{board.odd_lot_label}</TableCell><TableCell className="text-right font-mono text-[12px]">{number(board.limit_pct * 100, 0)}%</TableCell><TableCell className="text-right font-mono text-[12px]">{board.cage_label}</TableCell></TableRow>;
                      })}
                    </TableBody>
                  </Table>
                  {rulesOpen && <div className="rounded-lg border border-warning/20 bg-warning/5 p-2.5 text-[11px]"><strong className="block text-warning">未建模项（如实标注，不伪称覆盖）</strong><ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted-foreground">{(tradingRules?.not_modeled ?? []).map((item) => <li key={item}>{item}</li>)}</ul><small className="mt-1 block text-muted-foreground">规则依据：沪深交易所《交易规则》（2026年修订，2026-07-06 施行）分板块口径</small></div>}
                </div>
              )
            ) : !tradingRules ? (
              rulesLoading ? <Loading label="正在加载交易规则" /> : <div className="flex items-start gap-1.5 rounded-lg bg-muted/50 px-3 py-2 text-[11px] text-muted-foreground"><ShieldCheck className="mt-0.5 size-3.5 shrink-0" /><span>输入 6 位标的代码，或在「委托下单 · 选股结果」中点击候选，即自动加载所属板块的交易规则。</span></div>
            ) : (
              <div className="grid gap-2">
                <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
                  {[
                    ['最小申报', `${integer(tradingRules.rules.min_qty)} 股`],
                    ['递增单位', tradingRules.rules.step === 1 ? '1 股' : `${integer(tradingRules.rules.step)} 整数倍`],
                    ['限价上限', tradingRules.rules.limit_order_cap > 0 ? `${integer(tradingRules.rules.limit_order_cap)} 股` : '未建模'],
                    ['市价上限', tradingRules.rules.market_order_cap > 0 ? `${integer(tradingRules.rules.market_order_cap)} 股` : '未建模'],
                    ['零股卖出', tradingRules.rules.odd_lot_label],
                    ['涨跌幅', `${number(tradingRules.rules.limit_pct * 100, 0)}%`],
                    ['价格笼子', tradingRules.rules.cage_label],
                  ].map(([label, value]) => <div key={label} className="rounded-lg border bg-muted/30 px-2.5 py-2"><span className="block text-[10px] text-muted-foreground">{label}</span><strong className="block font-mono text-[12px]">{value}</strong>{label === '价格笼子' && tradingRules.price_cage && <small className="block text-[10px] text-muted-foreground">当前 {money(tradingRules.price_cage.lower)} ~ {money(tradingRules.price_cage.upper)} 元</small>}</div>)}
                </div>
                {rulesOpen && <div className="rounded-lg border border-warning/20 bg-warning/5 p-2.5 text-[11px]"><strong className="block text-warning">未建模项（如实标注，不伪称覆盖）</strong><ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted-foreground">{tradingRules.not_modeled.map((item) => <li key={item}>{item}</li>)}</ul><small className="mt-1 block text-muted-foreground">规则依据：{tradingRules.rules.rule_basis}</small></div>}
              </div>
            )}
          </Panel>
        </>
      )}

      <Panel title="交易费用配置" description="按万分之自定义五项费率与最低佣金，可保存为全局默认或应用到当前模拟账户">
        <FeeConfigPanel value={fees} onChange={setFees} accountId={account?.id} onAccountUpdated={(state) => { setAccount(state); toast.success('当前账户的费用配置已更新。'); }} />
      </Panel>

      <ConfirmDialog
        open={pendingReset}
        onOpenChange={setPendingReset}
        title="重置虚拟账户"
        description="当前持仓、订单和成交记录将被清除，此操作无法撤销。"
        confirmLabel="重置"
        destructive
        busy={resetting}
        onConfirm={resetAccount}
      />
    </div>
  );
}


