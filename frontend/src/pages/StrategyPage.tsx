import {
  AlertOctagon, AlertTriangle, ArrowDown, ArrowRight, BadgeCheck, Ban, BookOpenCheck, Check,
  CircleDot, GitBranch, Layers3, LockKeyhole, Repeat2, Scale, ShieldAlert, Split, TimerReset, WalletCards,
} from 'lucide-react';
import { Badge, PageHeader, Panel } from '../components/Ui';

const groups = [
  { label: '探索层', range: '01—04', amount: 10, tone: 'layer-one', note: '低风险启动' },
  { label: '确认层', range: '05—08', amount: 20, tone: 'layer-two', note: '分批确认' },
  { label: '扩展层', range: '09—12', amount: 30, tone: 'layer-three', note: '逐级扩展' },
  { label: '核心层', range: '13—16', amount: 40, tone: 'layer-four', note: '末段投入' },
];

export default function StrategyPage() {
  return (
    <div className="page strategy-page">
      <PageHeader
        eyebrow="STRATEGY / SPECIFICATION"
        title="策略规则"
        description="将金额序列、双账本与退出状态机固化为可审阅、可验证的执行契约。"
        actions={<div className="version-badge"><BadgeCheck size={16} /><div><strong>用户定稿版本</strong><span>2026-11-11</span></div></div>}
      />
      <div className="spec-notice"><BookOpenCheck size={17} /><p>本页规则来自用户于 <strong>2026-11-11</strong> 定稿的策略说明。做 T 为可选增强模块，<strong>不占用 16 笔金额序列的笔位</strong>。</p><Badge tone="indigo">SPEC LOCKED</Badge></div>

      <div className="rule-summary-grid">
        <div><span>16</span><div><strong>笔分层投入</strong><small>固定金额序列</small></div><Layers3 size={20} /></div>
        <div><span>02</span><div><strong>套独立账本</strong><small>永久仓 / T 仓</small></div><Scale size={20} /></div>
        <div><span>03</span><div><strong>个止盈状态</strong><small>Idle / Armed / Exit</small></div><GitBranch size={20} /></div>
        <div><span>T+1</span><div><strong>可卖约束</strong><small>先校验，再清仓</small></div><TimerReset size={20} /></div>
      </div>

      <Panel className="sequence-panel" title="16 笔金额序列" description="四个层级、每级四笔；序号表达投入顺序，不代表收益预测" action={<span className="sequence-total">序列名义总额 <strong>400 万元</strong></span>}>
        <div className="sequence-axis"><span>资金投入顺序</span><div /><ArrowRight size={15} /></div>
        <div className="sequence-groups">
          {groups.map((group, groupIndex) => (
            <div className={`sequence-group ${group.tone}`} key={group.label}>
              <div className="sequence-group-header"><div><span>L{groupIndex + 1}</span><strong>{group.label}</strong></div><small>{group.note}</small></div>
              <div className="sequence-slots">
                {[0, 1, 2, 3].map((offset) => <div className="sequence-slot" key={offset}><span>#{String(groupIndex * 4 + offset + 1).padStart(2, '0')}</span><strong>{group.amount}<small>万</small></strong><div className="slot-meter"><i style={{ width: `${group.amount * 2.5}%` }} /></div><small>固定笔位</small></div>)}
              </div>
              <div className="sequence-group-footer"><span>笔位 {group.range}</span><strong>{group.amount} 万 × 4 = {group.amount * 4} 万</strong></div>
            </div>
          ))}
        </div>
        <div className="sequence-caption"><CircleDot size={14} /><span>金额层级为 10 × 4 / 20 × 4 / 30 × 4 / 40 × 4 万元。实际是否投入及成交数量由后端策略状态、可用资金与撮合约束决定。</span></div>
      </Panel>

      <div className="strategy-two-col">
        <Panel className="ledger-panel" title="永久仓与 T 仓双账本" description="同一代码、不同经济目的，成本与在途数量分开核算" action={<WalletCards size={19} />}>
          <div className="ledger-cards">
            <div className="ledger-card permanent"><div className="ledger-card-title"><span><LockKeyhole size={18} /></span><div><strong>永久仓账本</strong><small>PERMANENT LEDGER</small></div><Badge>底仓</Badge></div><p>记录策略长期持有的核心数量与独立成本，不因 T 仓短期进出而混入摊薄。</p><dl><div><dt>核心字段</dt><dd>permanent_qty</dd></div><div><dt>成本口径</dt><dd>permanent_cost</dd></div><div><dt>卖出约束</dt><dd>sellable_qty</dd></div></dl></div>
            <div className="ledger-card t-ledger"><div className="ledger-card-title"><span><Repeat2 size={18} /></span><div><strong>T 仓账本</strong><small>TACTICAL LEDGER</small></div><Badge tone="indigo">可选</Badge></div><p>单独记录做 T 在途数量与净贡献；启用与否由 enable_t 控制，且不占固定笔位。</p><dl><div><dt>核心字段</dt><dd>t_active_qty</dd></div><div><dt>损益口径</dt><dd>t_net_profit</dd></div><div><dt>笔位占用</dt><dd>不占用</dd></div></dl></div>
          </div>
          <div className="ledger-equation"><Scale size={16} /><span>总持仓 = 永久仓数量 + T 仓在途数量</span><strong>成本分账 · 风险合并观察</strong></div>
        </Panel>

        <Panel className="allocation-panel" title="建仓分流" description="买入事件先识别资金目的，再进入对应账本" action={<Split size={19} />}>
          <div className="flow-diagram vertical-flow">
            <div className="flow-node primary-node"><span>01</span><div><strong>建仓买入事件</strong><small>校验可用资金、价格与数量</small></div></div>
            <div className="flow-connector"><ArrowDown size={17} /></div>
            <div className="flow-node"><span>02</span><div><strong>读取当前序列笔位</strong><small>16 笔序列仅在永久仓建仓时推进</small></div></div>
            <div className="flow-connector"><ArrowDown size={17} /></div>
            <div className="flow-branch"><div><GitBranch size={18} /><strong>按资金目的分流</strong></div><div className="branch-lines"><i /><i /></div><div className="branch-nodes"><span><LockKeyhole size={15} />永久仓<small>占用下一笔位</small></span><span><Repeat2 size={15} />T 仓<small>可选 · 不占笔位</small></span></div></div>
          </div>
        </Panel>
      </div>

      <Panel className="state-panel" title="双轨止盈状态机" description="永久仓与 T 仓各自跟踪触发条件；状态转换以策略后端事件为准" action={<div className="state-legend"><span className="idle" />Idle<span className="armed" />Armed<span className="exit" />Exit</div>}>
        <div className="state-machine">
          <div className="state-node idle"><div className="state-node-top"><span>01</span><Badge>观察</Badge></div><CircleDot size={26} /><h3>Idle</h3><strong>空闲 / 等待</strong><p>持有账本并监测行情，尚未形成退出信号。新轮次从此状态开始。</p><div className="state-condition">输入：持仓 + 新行情</div></div>
          <div className="state-arrow"><ArrowRight size={22} /><span>达到触发条件</span></div>
          <div className="state-node armed"><div className="state-node-top"><span>02</span><Badge tone="amber">确认</Badge></div><AlertOctagon size={26} /><h3>Armed</h3><strong>已触发 / 武装</strong><p>进入止盈确认区，持续检查回撤或退出条件，而非等同立即成交。</p><div className="state-condition">输入：止盈触发事件</div></div>
          <div className="state-arrow"><ArrowRight size={22} /><span>满足退出条件</span></div>
          <div className="state-node exit"><div className="state-node-top"><span>03</span><Badge tone="red">执行</Badge></div><LogOutIcon /><h3>Exit</h3><strong>退出 / 清仓</strong><p>按可卖约束生成退出委托，成交后结算对应账本并记录完整事件。</p><div className="state-condition">输入：退出确认 + 可卖校验</div></div>
        </div>
        <div className="state-footnote"><Repeat2 size={14} /><span>未满足退出条件时的回退或重新等待由策略规则与后端事件流决定；前端不自行推断状态转换。</span></div>
      </Panel>

      <div className="strategy-two-col lower-rules">
        <Panel className="liquidation-panel" title="T+1 清仓顺序" description="先确定当日可卖边界，再按账本目的执行" action={<TimerReset size={19} />}>
          <ol className="liquidation-steps">
            <li><span>1</span><div><strong>冻结当日不可卖数量</strong><p>当日买入部分不进入当日可卖集合，先以 sellable_qty 校验。</p></div><Badge>T+1</Badge></li>
            <li><span>2</span><div><strong>优先处理 T 仓可卖部分</strong><p>结算 T 仓在途交易及独立净贡献，不消耗永久仓建仓笔位。</p></div><Repeat2 size={17} /></li>
            <li><span>3</span><div><strong>再处理永久仓可卖部分</strong><p>依据 Exit 状态执行永久仓退出，保持永久仓成本独立核算。</p></div><LockKeyhole size={17} /></li>
            <li><span>4</span><div><strong>核对残余并结束轮次</strong><p>未成交或不可卖部分保留在账本，不能在前端假定已全部清仓。</p></div><Check size={17} /></li>
          </ol>
        </Panel>

        <Panel className="risk-rules-panel" title="熔断与止损风险边界" description="风控事件需要价格与流动性配合，规则触发不等于成交保证" action={<ShieldAlert size={19} />}>
          <div className="risk-rule"><span className="risk-rule-icon"><Ban size={19} /></span><div><h3>事件熔断</h3><p>指数或预设事件触发后，暂停相应新建仓动作并记录事件；是否恢复由后续规则判定。</p><div className="risk-tags"><Badge tone="amber">暂停新增风险</Badge><Badge>保留事件记录</Badge></div></div></div>
          <div className="risk-rule critical"><span className="risk-rule-icon"><AlertTriangle size={19} /></span><div><h3>无价格止损风险</h3><p>没有可用价格、行情中断或流动性不足时，止损信号可能无法转化为有效委托与成交。</p><div className="risk-tags"><Badge tone="red">不保证止损价</Badge><Badge tone="red">不保证成交</Badge></div></div></div>
          <div className="risk-bottom-line"><AlertOctagon size={15} /><span>回测必须保留此类 warning；不得将理论止损价直接视为实际成交价。</span></div>
        </Panel>
      </div>

      <div className="spec-footer"><span>LayerLab Strategy Specification</span><div><i />用户定稿 · 2026-11-11<i />做 T 可选，不占笔位</div></div>
    </div>
  );
}

function LogOutIcon() {
  return <ArrowRight size={26} />;
}
