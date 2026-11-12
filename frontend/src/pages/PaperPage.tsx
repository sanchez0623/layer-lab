import { useEffect, useRef, useState } from 'react';
import {
  Activity, ArrowDownLeft, ArrowUpRight, CircleDollarSign, FastForward, Landmark, Pause, Play,
  Plus, Radio, RefreshCw, RotateCcw, Send, ShieldCheck, StepForward, Timer, Wallet, XCircle,
} from 'lucide-react';
import { ApiError, api } from '../lib/api';
import { cleanSymbolInput, dateTime, displaySymbol, integer, lower, money, number, orderTypeLabel, purposeLabel, sideLabel, signedMoney, statusLabel } from '../lib/format';
import FeeConfigPanel from '../components/FeeConfigPanel';
import { Badge, EmptyState, ErrorBanner, Field, Loading, Metric, PageHeader, Panel, SyntheticNotice, TableEmpty } from '../components/Ui';
import { defaultFeeConfig, feeBreakdownTitle, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import type { AccountState, Dataset, FeeConfig, OrderSide, OrderType, StrategyConfig } from '../types';

const defaultConfig: StrategyConfig = {
  enable_t: true, commission_rate: 0.00005, min_commission: 5, stamp_tax_rate: 0.0005,
  handling_fee_rate: 0.0000341, regulatory_fee_rate: 0.00002, transfer_fee_rate: 0.00001,
  slippage_bps: 1, volume_participation: 0.2,
};

export default function PaperPage() {
  const [account, setAccount] = useState<AccountState | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [config, setConfig] = useState<StrategyConfig>(defaultConfig);
  const [fees, setFees] = useState<FeeConfig>(defaultFeeConfig);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [accountName, setAccountName] = useState('本地模拟账户');
  const [initialCash, setInitialCash] = useState(4000000);
  const [datasetId, setDatasetId] = useState('');
  const [autoStrategy, setAutoStrategy] = useState(false);
  const [togglingAuto, setTogglingAuto] = useState(false);
  const [creating, setCreating] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [playing, setPlaying] = useState(false);
  const advancingRef = useRef(false);
  const [liveLoading, setLiveLoading] = useState(false);
  const [livePlaying, setLivePlaying] = useState(false);
  const [side, setSide] = useState<OrderSide>('buy');
  const [orderType, setOrderType] = useState<OrderType>('market');
  const [symbol, setSymbol] = useState('');
  const [quantity, setQuantity] = useState(100);
  const [limitPrice, setLimitPrice] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState<string | number | null>(null);
  const [resetting, setResetting] = useState(false);
  const [recordTab, setRecordTab] = useState<'orders' | 'trades'>('orders');

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
      if (daily) { setDatasetId((current) => current || daily.id); setSymbol((current) => current || cleanSymbolInput(daily.symbol)); }
      try {
        const current = await api.currentAccount();
        setAccount(current);
        setAutoStrategy(Boolean(current.auto_strategy));
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

  const liveTick = async (fromTimer = false) => {
    const code = cleanSymbolInput(symbol);
    if (!account || code.length !== 6) return;
    if (!fromTimer) setLiveLoading(true);
    setError('');
    try {
      const next = await api.liveTick(account.id, code);
      setAccount(next);
      setSuccess('已按腾讯实时快照刷新本地撮合账户。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '腾讯实时行情刷新失败。');
      if (fromTimer) setLivePlaying(false);
    } finally {
      if (!fromTimer) setLiveLoading(false);
    }
  };

  useEffect(() => {
    if (!livePlaying || !account) return;
    const timer = window.setInterval(() => void liveTick(true), 60000);
    return () => window.clearInterval(timer);
  }, [livePlaying, account?.id, symbol]);

  const createAccount = async () => {
    if (!accountName.trim() || initialCash <= 0) { setError('请填写账户名称，并确保初始资金大于 0。'); return; }
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
      setSuccess('虚拟账户已创建，可以开始逐根推送行情。');
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
      setSuccess(next.auto_strategy ? '自动策略已开启：将复用回测策略状态机。' : '自动策略已关闭：仅保留手工委托撮合。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '自动策略切换失败。');
    } finally {
      setTogglingAuto(false);
    }
  };

  const placeOrder = async () => {
    if (!account) return;
    const code = cleanSymbolInput(symbol);
    if (code.length !== 6) { setError('请输入 6 位 A 股代码，例如 688200；沪深交易所由后端自动识别。'); return; }
    if (!Number.isInteger(quantity) || quantity <= 0) { setError('请填写正整数委托数量。'); return; }
    if (orderType === 'limit' && (!Number(limitPrice) || Number(limitPrice) <= 0)) { setError('限价委托需要填写大于 0 的委托价格。'); return; }
    setSubmitting(true); setError(''); setSuccess('');
    try {
      const result = await api.placeOrder(account.id, {
        symbol: code, side, order_type: orderType, quantity,
        limit_price: orderType === 'limit' ? Number(limitPrice) : undefined,
      });
      setAccount(result.account);
      setSuccess(`${sideLabel(side)}委托已提交，状态：${statusLabel(result.order.status)}。`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '委托提交失败。'); }
    finally { setSubmitting(false); }
  };

  const cancelOrder = async (id: string | number) => {
    if (!account) return;
    setCancelling(id); setError('');
    try { setAccount(await api.cancelOrder(account.id, id)); setSuccess('委托已撤销，账户状态已同步。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '撤单失败。'); }
    finally { setCancelling(null); }
  };

  const resetAccount = async () => {
    if (!account || !window.confirm('确定重置此虚拟账户吗？\n\n当前持仓、订单和成交记录将被清除，此操作无法撤销。')) return;
    setResetting(true); setError(''); setSuccess(''); setPlaying(false);
    try { setAccount(await api.resetAccount(account.id)); setSuccess('虚拟账户已恢复至初始状态。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '账户重置失败。'); }
    finally { setResetting(false); }
  };

  const currentDataset = datasets.find((item) => item.id === account?.dataset_id);
  const canCancel = (status: string) => ['open', 'pending', 'partial', 'partially_filled', 'new'].includes(status.toLowerCase());

  return (
    <div className="page paper-page">
      <PageHeader
        eyebrow="EXECUTION / PAPER"
        title="模拟交易"
        description="接入腾讯 1 分钟实时快照，观察委托、T+1 可卖约束与双账本状态。"
        actions={account ? <button className="button secondary" onClick={() => void refreshAccount(account.id)} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''} />刷新账户</button> : undefined}
      />
      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}
      {success && <div className="inline-success"><ShieldCheck size={17} /><span>{success}</span><button className="text-button" onClick={() => setSuccess('')}>知道了</button></div>}

      {loading && !account ? <Panel><Loading label="正在读取模拟账户" /></Panel> : !account ? (
        <Panel className="account-create-panel">
          <div className="account-create-intro"><div className="create-icon"><Landmark size={25} /></div><div><h2>建立本地虚拟账户</h2><p>账户仅用于规则与撮合验证，不连接券商，不产生真实资金变动。</p></div></div>
          <div className="form-grid create-account-form">
            <Field label="账户名称"><input value={accountName} onChange={(event) => setAccountName(event.target.value)} /></Field>
            <Field label="初始资金"><div className="input-with-unit"><input type="number" min="1" step="10000" value={initialCash} onChange={(event) => setInitialCash(Number(event.target.value))} /><span>CNY</span></div></Field>
            <Field label="行情数据集" hint="用于逐根推送"><select value={datasetId} onChange={(event) => { setDatasetId(event.target.value); const selected = datasets.find((item) => item.id === event.target.value); if (selected) setSymbol(cleanSymbolInput(selected.symbol)); }}><option value="">暂不绑定</option>{datasets.map((item) => <option key={item.id} value={item.id}>{item.name} · {displaySymbol(item.symbol)} · {item.timeframe}</option>)}</select></Field>
            <label className="switch-row span-2 create-auto-switch"><span><strong>自动执行策略</strong><small>复用回测LayeredTStrategy状态机</small></span><input type="checkbox" checked={autoStrategy} onChange={(event) => setAutoStrategy(event.target.checked)} /><span className="switch" /></label>
            <button className="button primary create-account-button" onClick={() => void createAccount()} disabled={creating}>{creating ? <span className="button-spinner" /> : <Plus size={16} />}{creating ? '正在创建' : '创建虚拟账户'}</button>
          </div>
        </Panel>
      ) : account && (
        <>
          <div className="account-context-bar">
            <div><span className="account-avatar">{account.name.slice(0, 1).toUpperCase()}</span><div><strong>{account.name}</strong><span>ID {account.id.slice(0, 12)} · 游标 {integer(account.cursor)}</span></div></div>
            <div className="account-context-right"><button className="button secondary" onClick={() => void toggleAutoStrategy()} disabled={togglingAuto}><RefreshCw size={14} className={togglingAuto ? 'spin' : ''} />{account.auto_strategy ? '自动策略开启' : '自动策略关闭'}</button>{currentDataset?.is_synthetic && <SyntheticNotice compact />}<Badge tone={lower(account.status) === 'active' ? 'green' : 'neutral'}>{statusLabel(account.status)}</Badge><span>状态机 <b>{account.armed_state}</b></span><span>序列 <b>{account.sequence_index} / 16</b></span></div>
          </div>
          <div className="metrics-grid four account-metrics">
            <Metric label="账户权益" value={money(account.equity)} icon={<Wallet size={15} />} sub={`初始资金 ${money(account.initial_cash, 0)}`} />
            <Metric label="可用资金" value={money(account.available_cash)} sub={`账面现金 ${money(account.cash)}`} />
            <Metric label="持仓市值" value={money(account.market_value)} sub={`敞口 ${account.equity ? number(account.market_value / account.equity * 100, 1) : '0.0'}%`} />
            <Metric label="已实现盈亏" value={signedMoney(account.realized_pnl)} tone={account.realized_pnl >= 0 ? 'positive' : 'negative'} sub={`做 T 净贡献 ${signedMoney(account.t_net_profit)}`} />
          </div>

          <div className="paper-top-grid">
            <Panel className="quote-panel" title="腾讯实时行情" description={currentDataset ? `${currentDataset.name} · ${displaySymbol(currentDataset.symbol)} · ${currentDataset.timeframe}` : '直接读取腾讯快照，无需绑定缓存数据集'} action={<Badge tone="indigo">1 MIN</Badge>}>
              <div className="quote-list">
                {account.positions.length === 0 ? <EmptyState compact icon={<Activity size={22} />} title="等待实时快照" description="输入标的并拉取腾讯行情后，本地账户会显示最新价；不会用前端行情替代后端状态。" /> : account.positions.map((position) => (
                  <div className="quote-row" key={position.symbol}><div><strong title={position.symbol}>{displaySymbol(position.symbol)}</strong><span>{position.total_qty > 0 ? `持仓 ${integer(position.total_qty)}` : '最新行情'}</span></div><div><strong>{money(position.last_price)}</strong><span>后端最新价</span></div></div>
                ))}
              </div>
              <div className="live-controls">
                <button className="button secondary" disabled={liveLoading || livePlaying || cleanSymbolInput(symbol).length !== 6} onClick={() => void liveTick()}>
                  {liveLoading ? <span className="button-spinner" /> : <Radio size={16} />}拉取实时快照
                </button>
                <button className={`button ${livePlaying ? 'danger-soft' : 'primary'}`} disabled={cleanSymbolInput(symbol).length !== 6} onClick={() => setLivePlaying(!livePlaying)}>
                  {livePlaying ? <Pause size={15} /> : <Play size={15} fill="currentColor" />}{livePlaying ? '暂停自动刷新' : '每60秒自动刷新'}
                </button>
              </div>
              <div className="playback-note"><Timer size={13} className="note-icon" />{livePlaying ? '已启用腾讯实时快照，每 60 秒刷新一次' : '实时刷新已暂停 · 快照来自腾讯行情接口'}</div>
              <div className="playback-divider"><span>缓存 1 分钟回放</span><small>用于批量验证撮合与订单生命周期</small></div>
              <div className="playback-controls">
                <button className="button secondary" disabled={advancing || playing || !account.dataset_id} onClick={() => void advance(1)}><StepForward size={16} />推送 1 根</button>
                <button className="button secondary" disabled={advancing || playing || !account.dataset_id} onClick={() => void advance(10)}><FastForward size={16} />推送 10 根</button>
                <button className={`button ${playing ? 'danger-soft' : 'secondary'}`} disabled={!account.dataset_id} onClick={() => setPlaying(!playing)}>{playing ? <Pause size={15} /> : <Play size={15} fill="currentColor" />}{playing ? '暂停回放' : '加速回放'}</button>
              </div>
              <div className="playback-note"><span className={playing ? 'live-dot playing' : 'live-dot'} />{playing ? '回放模式：每 1.4 秒推进 1 根缓存 1 分钟K线' : advancing ? '正在推送行情并同步账户' : '回放已暂停 · 行情由后端逐根推进'}</div>
            </Panel>

            <Panel className="order-ticket" title="委托下单" description="订单将提交至本地仿真撮合引擎" action={<span className="ticket-label">ORDER TICKET</span>}>
              <div className="side-selector"><button className={side === 'buy' ? 'buy active' : 'buy'} onClick={() => setSide('buy')}><ArrowDownLeft size={16} />买入</button><button className={side === 'sell' ? 'sell active' : 'sell'} onClick={() => setSide('sell')}><ArrowUpRight size={16} />卖出</button></div>
              <div className="order-type-row"><span>委托方式</span><div className="segmented"><button className={orderType === 'market' ? 'active' : ''} onClick={() => setOrderType('market')}>市价</button><button className={orderType === 'limit' ? 'active' : ''} onClick={() => setOrderType('limit')}>限价</button></div></div>
              <div className="ticket-fields">
                <Field label="标的代码" hint="6 位数字，自动识别沪深"><input value={symbol} onChange={(event) => setSymbol(cleanSymbolInput(event.target.value))} placeholder="例如 688200" inputMode="numeric" maxLength={6} /></Field>
                <Field label="委托数量"><div className="input-with-unit"><input type="number" min="1" step="100" value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} /><span>股</span></div></Field>
                {orderType === 'limit' && <Field label="委托价格" className="span-2"><div className="input-with-unit"><input type="number" min="0.01" step="0.01" value={limitPrice} onChange={(event) => setLimitPrice(event.target.value)} placeholder="0.00" /><span>元</span></div></Field>}
              </div>
              <div className="ticket-estimate"><span>可用资金 <b>{money(account.available_cash)}</b></span>{orderType === 'limit' && limitPrice && <span>委托金额约 <b>{money(Number(limitPrice) * quantity)}</b></span>}</div>
              <button className={`button order-submit ${side}`} disabled={submitting} onClick={() => void placeOrder()}>{submitting ? <span className="button-spinner" /> : <Send size={16} />}{submitting ? '正在提交委托' : `提交${sideLabel(side)}委托`}</button>
              <p className="ticket-disclaimer">虚拟委托，不会发送至任何真实交易市场。</p>
            </Panel>
          </div>

          <Panel className="positions-panel" title="持仓与双账本" description="永久仓和 T 仓分账记录，可卖数量遵循 T+1 约束" action={<span className="fee-note" title="佣金 + 印花税 + 经手费 + 证管费 + 过户费；滑点体现在成交价，不计入费用">累计费用 {money(account.total_fees)}（五项合计）</span>}>
            <div className="table-scroll"><table><thead><tr><th>标的</th><th className="num">最新价</th><th className="num">总持仓</th><th className="num">可卖数量</th><th className="num">永久仓</th><th className="num">永久仓成本</th><th className="num">平均成本</th><th className="num">T 仓在途</th><th className="num">持仓市值</th><th className="num">浮动盈亏</th></tr></thead><tbody>
              {account.positions.length === 0 ? <TableEmpty colSpan={10} message="暂无持仓，提交买入委托并推送行情后查看" /> : account.positions.map((position) => {
                const floatPnl = position.float_pnl;
                return <tr key={position.symbol}><td><strong title={position.symbol}>{displaySymbol(position.symbol)}</strong></td><td className="num">{money(position.last_price)}</td><td className="num"><strong>{integer(position.total_qty)}</strong></td><td className="num">{integer(position.sellable_qty)}</td><td className="num">{integer(position.permanent_qty)}</td><td className="num">{money(position.permanent_cost)}</td><td className="num">{money(position.average_cost)}</td><td className="num">{position.t_active_qty > 0 ? <Badge tone="indigo">{integer(position.t_active_qty)}</Badge> : '0'}</td><td className="num">{money(position.market_value)}</td><td className={`num ${floatPnl >= 0 ? 'positive' : 'negative'}`}>{signedMoney(floatPnl)}</td></tr>;
              })}
            </tbody></table></div>
          </Panel>

          <Panel className="records-panel">
            <div className="records-header"><div className="tabs" role="tablist"><button className={recordTab === 'orders' ? 'active' : ''} onClick={() => setRecordTab('orders')}>订单<span>{account.orders.length}</span></button><button className={recordTab === 'trades' ? 'active' : ''} onClick={() => setRecordTab('trades')}>成交<span>{account.trades.length}</span></button></div><button className="button danger-text" onClick={() => void resetAccount()} disabled={resetting}><RotateCcw size={14} className={resetting ? 'spin' : ''} />{resetting ? '正在重置' : '重置虚拟账户'}</button></div>
            <div className="table-scroll">
              {recordTab === 'orders' ? <table><thead><tr><th>委托时间</th><th>标的</th><th>方向</th><th>类型</th><th className="num">限价</th><th className="num">委托数量</th><th className="num">已成交</th><th>状态</th><th className="action-col">操作</th></tr></thead><tbody>
                {account.orders.length === 0 ? <TableEmpty colSpan={9} message="暂无订单记录" /> : account.orders.map((order) => <tr key={order.id}><td>{dateTime(order.created_at || order.timestamp)}</td><td><strong title={order.symbol}>{displaySymbol(order.symbol)}</strong></td><td><span className={`side-label ${lower(order.side) === 'buy' ? 'buy' : 'sell'}`}>{sideLabel(order.side)}</span></td><td>{orderTypeLabel(order.order_type || order.type)}</td><td className="num">{money(order.limit_price ?? order.price)}</td><td className="num">{integer(order.quantity)}</td><td className="num">{integer(order.filled_quantity ?? order.filled_qty)}</td><td><Badge tone={lower(order.status) === 'filled' ? 'green' : lower(order.status) === 'rejected' ? 'red' : 'neutral'}>{statusLabel(order.status)}</Badge></td><td className="action-col">{canCancel(order.status) ? <button className="table-action" disabled={cancelling === order.id} onClick={() => void cancelOrder(order.id)}>{cancelling === order.id ? <RefreshCw size={13} className="spin" /> : <XCircle size={13} />}撤单</button> : <span className="muted">—</span>}</td></tr>)}
              </tbody></table> : <table><thead><tr><th>成交时间</th><th>标的</th><th>方向</th><th>用途</th><th className="num">价格</th><th className="num">数量</th><th className="num">成交额</th><th className="num">费用</th><th className="num">已实现盈亏</th></tr></thead><tbody>
                {account.trades.length === 0 ? <TableEmpty colSpan={9} message="暂无成交记录" /> : account.trades.map((trade, index) => <tr key={String(trade.id ?? index)}><td>{dateTime(trade.timestamp)}</td><td><strong title={trade.symbol}>{displaySymbol(trade.symbol)}</strong></td><td><span className={`side-label ${lower(trade.side) === 'buy' ? 'buy' : 'sell'}`}>{sideLabel(trade.side)}</span></td><td>{purposeLabel(trade.purpose)}</td><td className="num">{money(trade.price)}</td><td className="num">{integer(trade.quantity)}</td><td className="num">{money(trade.amount ?? trade.notional)}</td><td className="num" title={feeBreakdownTitle(trade)}>{money(trade.total_fee ?? trade.fee)}</td><td className={`num ${(trade.realized_pnl || 0) >= 0 ? 'positive' : 'negative'}`}>{trade.realized_pnl === undefined ? '—' : signedMoney(trade.realized_pnl)}</td></tr>)}
              </tbody></table>}
            </div>
          </Panel>
          <div className="paper-disclosure"><CircleDollarSign size={15} /><span>账户权益与盈亏均由本地后端仿真状态计算。模拟结果不代表实盘表现，也不构成收益承诺。</span></div>
        </>
      )}
      <Panel className="fee-settings-panel" title="交易费用配置" description="按万分之自定义五项费率与最低佣金，可保存为全局默认或应用到当前模拟账户">
        <FeeConfigPanel
          value={fees}
          onChange={setFees}
          accountId={account?.id}
          onAccountUpdated={(state) => { setAccount(state); setSuccess('当前账户的费用配置已更新。'); }}
        />
      </Panel>
    </div>
  );
}
