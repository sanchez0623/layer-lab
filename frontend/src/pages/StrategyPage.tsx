import {
  AlertOctagon, AlertTriangle, ArrowDown, ArrowRight, BadgeCheck, Ban, BookOpenCheck, Check,
  CircleDot, GitBranch, Layers3, LockKeyhole, Repeat2, Scale, ShieldAlert, Split, TimerReset, WalletCards,
} from 'lucide-react';
import { Badge, PageHeader, Panel } from '../components/Ui';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Card } from '@/components/ui/card';

const groups = [
  { label: '探索层', range: '01—04', amount: 10, note: '低风险启动', tone: 'bg-sky-500/10 text-sky-700' },
  { label: '确认层', range: '05—08', amount: 20, note: '分批确认', tone: 'bg-indigo-500/10 text-indigo-700' },
  { label: '扩展层', range: '09—12', amount: 30, note: '逐级扩展', tone: 'bg-amber-500/10 text-amber-700' },
  { label: '核心层', range: '13—16', amount: 40, note: '末段投入', tone: 'bg-rose-500/10 text-rose-700' },
];

const stateNodes = [
  { key: 'Idle', icon: CircleDot, tone: 'border-border bg-card', badge: <Badge>观察</Badge>, desc: '持有账本并监测行情，尚未形成退出信号。新轮次从此状态开始。', input: '输入：持仓 + 新行情' },
  { key: 'Armed', icon: AlertOctagon, tone: 'border-warning/30 bg-warning/5', badge: <Badge tone="amber">确认</Badge>, desc: '进入止盈确认区，持续检查回撤或退出条件，而非等同立即成交。', input: '输入：止盈触发事件' },
  { key: 'Exit', icon: ArrowRight, tone: 'border-destructive/25 bg-destructive/5', badge: <Badge tone="red">执行</Badge>, desc: '按可卖约束生成退出委托，成交后结算对应账本并记录完整事件。', input: '输入：退出确认 + 可卖校验' },
];

export default function StrategyPage() {
  return (
    <div className="animate-[page-in_.22s_ease-out]">
      <PageHeader
        eyebrow="STRATEGY / SPECIFICATION"
        title="策略规则"
        description="将金额序列、双账本与退出状态机固化为可审阅、可验证的执行契约。"
        actions={(
          <div className="flex items-center gap-2 rounded-lg border bg-card px-3 py-2">
            <BadgeCheck className="size-4 text-primary" />
            <div><strong className="block text-[12px]">用户定稿版本</strong><span className="block text-[11px] text-muted-foreground">2026-11-11</span></div>
          </div>
        )}
      />

      <div className="mb-3 flex flex-col gap-2 rounded-xl border border-primary/15 bg-accent/45 px-3.5 py-2.5 text-[13px] sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2">
          <BookOpenCheck className="mt-0.5 size-4 shrink-0 text-primary" />
          <p className="leading-6 text-muted-foreground">本页规则来自用户于 <strong className="text-foreground">2026-11-11</strong> 定稿的策略说明。做 T 为可选增强模块，<strong className="text-foreground">不占用 16 笔金额序列的笔位</strong>。</p>
        </div>
        <Badge tone="indigo">SPEC LOCKED</Badge>
      </div>

      <div className="mb-3 grid grid-cols-2 gap-2.5 xl:grid-cols-4">
        {[
          { value: '16', label: '笔分层投入', note: '固定金额序列', icon: Layers3 },
          { value: '02', label: '套独立账本', note: '永久仓 / T 仓', icon: Scale },
          { value: '03', label: '个止盈状态', note: 'Idle / Armed / Exit', icon: GitBranch },
          { value: 'T+1', label: '可卖约束', note: '先校验，再清仓', icon: TimerReset },
        ].map(({ value, label, note, icon: Icon }) => (
          <Card key={label} className="flex-row items-center gap-3 px-4 py-3 shadow-xs">
            <span className="font-mono text-2xl font-medium text-primary">{value}</span>
            <div className="min-w-0"><strong className="block text-[13px]">{label}</strong><small className="block text-[11px] text-muted-foreground">{note}</small></div>
            <Icon className="ml-auto size-5 text-muted-foreground/60" />
          </Card>
        ))}
      </div>

      <Panel title="16 笔金额序列" description="四个层级、每级四笔；序号表达投入顺序，不代表收益预测" action={<span className="text-xs text-muted-foreground">序列名义总额 <strong className="font-mono text-foreground">400 万元</strong></span>} className="mb-3">
        <div className="mb-3 flex items-center gap-2 text-[11px] text-muted-foreground"><span>资金投入顺序</span><div className="h-px flex-1 bg-border" /><ArrowRight className="size-3.5" /></div>
        <div className="grid gap-2.5 md:grid-cols-2 xl:grid-cols-4">
          {groups.map((group, groupIndex) => (
            <Card key={group.label} className="gap-2.5 px-3 py-3 shadow-none">
              <div className={`flex items-center justify-between rounded-md px-2 py-1.5 ${group.tone}`}>
                <div className="flex items-center gap-1.5"><span className="font-mono text-[11px]">L{groupIndex + 1}</span><strong className="text-[13px]">{group.label}</strong></div>
                <small className="text-[11px]">{group.note}</small>
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {[0, 1, 2, 3].map((offset) => (
                  <div key={offset} className="rounded-md border bg-muted/35 px-2 py-1.5">
                    <span className="font-mono text-[10px] text-muted-foreground">#{String(groupIndex * 4 + offset + 1).padStart(2, '0')}</span>
                    <strong className="block font-mono text-[15px]">{group.amount}<small className="ml-0.5 text-[10px] font-normal text-muted-foreground">万</small></strong>
                    <div className="mt-1 h-1 overflow-hidden rounded-full bg-border"><i className="block h-full rounded-full bg-primary/60" style={{ width: `${group.amount * 2.5}%` }} /></div>
                  </div>
                ))}
              </div>
              <div className="flex items-center justify-between border-t pt-2 text-[11px] text-muted-foreground">
                <span>笔位 {group.range}</span><strong className="text-foreground">{group.amount} 万 × 4 = {group.amount * 4} 万</strong>
              </div>
            </Card>
          ))}
        </div>
        <div className="mt-3 flex items-start gap-2 rounded-lg bg-muted/60 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
          <CircleDot className="mt-0.5 size-3.5 shrink-0" />
          <span>金额层级为 10 × 4 / 20 × 4 / 30 × 4 / 40 × 4 万元。实际是否投入及成交数量由后端策略状态、可用资金与撮合约束决定。</span>
        </div>
      </Panel>

      <div className="mb-3 grid gap-3 xl:grid-cols-2">
        <Panel title="永久仓与 T 仓双账本" description="同一代码、不同经济目的，成本与在途数量分开核算" action={<WalletCards className="size-4" />}>
          <div className="grid gap-2.5">
            <Card className="gap-2 border-primary/15 bg-accent/25 px-3.5 py-3 shadow-none">
              <div className="flex items-center gap-2.5">
                <span className="grid size-8 place-items-center rounded-md bg-card text-primary"><LockKeyhole className="size-4" /></span>
                <div className="min-w-0 flex-1"><strong className="block text-[13px]">永久仓账本</strong><small className="font-mono text-[10px] text-muted-foreground">PERMANENT LEDGER</small></div>
                <Badge>底仓</Badge>
              </div>
              <p className="text-[12px] leading-5 text-muted-foreground">记录策略长期持有的核心数量与独立成本，不因 T 仓短期进出而混入摊薄。</p>
              <dl className="grid grid-cols-3 gap-1.5 text-[11px]">
                {[['核心字段', 'permanent_qty'], ['成本口径', 'permanent_cost'], ['卖出约束', 'sellable_qty']].map(([k, v]) => <div key={k} className="rounded-md bg-card/70 px-2 py-1"><dt className="text-muted-foreground">{k}</dt><dd className="mt-0.5 font-mono text-[11px] text-foreground">{v}</dd></div>)}
              </dl>
            </Card>
            <Card className="gap-2 border-success/20 bg-success/5 px-3.5 py-3 shadow-none">
              <div className="flex items-center gap-2.5">
                <span className="grid size-8 place-items-center rounded-md bg-card text-success"><Repeat2 className="size-4" /></span>
                <div className="min-w-0 flex-1"><strong className="block text-[13px]">T 仓账本</strong><small className="font-mono text-[10px] text-muted-foreground">TACTICAL LEDGER</small></div>
                <Badge tone="indigo">可选</Badge>
              </div>
              <p className="text-[12px] leading-5 text-muted-foreground">单独记录做 T 在途数量与净贡献；启用与否由 enable_t 控制，且不占固定笔位。</p>
              <dl className="grid grid-cols-3 gap-1.5 text-[11px]">
                {[['核心字段', 't_active_qty'], ['损益口径', 't_net_profit'], ['笔位占用', '不占用']].map(([k, v]) => <div key={k} className="rounded-md bg-card/70 px-2 py-1"><dt className="text-muted-foreground">{k}</dt><dd className="mt-0.5 font-mono text-[11px] text-foreground">{v}</dd></div>)}
              </dl>
            </Card>
          </div>
          <div className="mt-2.5 flex items-center gap-2 rounded-lg bg-muted/60 px-3 py-2 text-[11px] text-muted-foreground"><Scale className="size-3.5" /><span>总持仓 = 永久仓数量 + T 仓在途数量</span><strong className="ml-auto text-foreground">成本分账 · 风险合并观察</strong></div>
        </Panel>

        <Panel title="建仓分流" description="买入事件先识别资金目的，再进入对应账本" action={<Split className="size-4" />}>
          <div className="flex flex-col items-stretch gap-2">
            <div className="flex items-center gap-2.5 rounded-lg border border-primary/20 bg-accent/35 px-3 py-2.5"><span className="font-mono text-[11px] text-primary">01</span><div><strong className="block text-[13px]">建仓买入事件</strong><small className="text-[11px] text-muted-foreground">校验可用资金、价格与数量</small></div></div>
            <div className="flex justify-center text-muted-foreground"><ArrowDown className="size-4" /></div>
            <div className="flex items-center gap-2.5 rounded-lg border bg-card px-3 py-2.5"><span className="font-mono text-[11px] text-muted-foreground">02</span><div><strong className="block text-[13px]">读取当前序列笔位</strong><small className="text-[11px] text-muted-foreground">16 笔序列仅在永久仓建仓时推进</small></div></div>
            <div className="flex justify-center text-muted-foreground"><ArrowDown className="size-4" /></div>
            <div className="rounded-lg border bg-muted/30 px-3 py-2.5">
              <div className="mb-2 flex items-center gap-2 text-[13px] font-medium"><GitBranch className="size-4 text-primary" />按资金目的分流</div>
              <div className="grid grid-cols-2 gap-2">
                <span className="flex items-center gap-2 rounded-md border bg-card px-2.5 py-2 text-[12px]"><LockKeyhole className="size-3.5 text-primary" />永久仓<small className="ml-auto text-[10px] text-muted-foreground">占用下一笔位</small></span>
                <span className="flex items-center gap-2 rounded-md border bg-card px-2.5 py-2 text-[12px]"><Repeat2 className="size-3.5 text-success" />T 仓<small className="ml-auto text-[10px] text-muted-foreground">可选 · 不占笔位</small></span>
              </div>
            </div>
          </div>
        </Panel>
      </div>

      <Panel title="双轨止盈状态机" description="永久仓与 T 仓各自跟踪触发条件；状态转换以策略后端事件为准" action={<div className="flex items-center gap-2 text-[11px] text-muted-foreground"><span className="size-2 rounded-full bg-muted-foreground/40" />Idle<span className="size-2 rounded-full bg-warning" />Armed<span className="size-2 rounded-full bg-destructive" />Exit</div>} className="mb-3">
        <div className="grid gap-2.5 lg:grid-cols-[1fr_auto_1fr_auto_1fr] lg:items-center">
          {stateNodes.map(({ key, icon: Icon, tone, badge, desc, input }, index) => (
            <div key={key} className="contents">
              <div className={`rounded-xl border p-3.5 ${tone}`}>
                <div className="mb-2 flex items-center justify-between"><span className="font-mono text-[11px] text-muted-foreground">0{index + 1}</span>{badge}</div>
                <Icon className="size-6 text-muted-foreground" />
                <h3 className="mt-2 text-[15px] font-semibold">{key}</h3>
                <strong className="block text-[12px] text-muted-foreground">{key === 'Idle' ? '空闲 / 等待' : key === 'Armed' ? '已触发 / 武装' : '退出 / 清仓'}</strong>
                <p className="mt-1 text-[12px] leading-5 text-muted-foreground">{desc}</p>
                <div className="mt-2 rounded-md border bg-card/60 px-2 py-1 text-[11px] text-muted-foreground">{input}</div>
              </div>
              {index < stateNodes.length - 1 && (
                <div className="flex flex-col items-center justify-center gap-1 text-muted-foreground">
                  <ArrowRight className="hidden size-5 lg:block" />
                  <ArrowDown className="size-5 lg:hidden" />
                  <span className="text-[10px]">{index === 0 ? '达到触发条件' : '满足退出条件'}</span>
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="mt-3 flex items-start gap-2 rounded-lg bg-muted/60 px-3 py-2 text-[11px] leading-5 text-muted-foreground"><Repeat2 className="mt-0.5 size-3.5 shrink-0" /><span>未满足退出条件时的回退或重新等待由策略规则与后端事件流决定；前端不自行推断状态转换。</span></div>
      </Panel>

      <div className="mb-3 grid gap-3 xl:grid-cols-2">
        <Panel title="T+1 清仓顺序" description="先确定当日可卖边界，再按账本目的执行" action={<TimerReset className="size-4" />}>
          <ol className="grid gap-2">
            {[
              ['冻结当日不可卖数量', '当日买入部分不进入当日可卖集合，先以 sellable_qty 校验。', <Badge key="t1">T+1</Badge>],
              ['优先处理 T 仓可卖部分', '结算 T 仓在途交易及独立净贡献，不消耗永久仓建仓笔位。', <Repeat2 key="t2" className="size-4 text-success" />],
              ['再处理永久仓可卖部分', '依据 Exit 状态执行永久仓退出，保持永久仓成本独立核算。', <LockKeyhole key="t3" className="size-4 text-primary" />],
              ['核对残余并结束轮次', '未成交或不可卖部分保留在账本，不能在前端假定已全部清仓。', <Check key="t4" className="size-4 text-success" />],
            ].map(([title, desc, icon], index) => (
              <li key={String(title)} className="flex items-start gap-2.5 rounded-lg border bg-card px-3 py-2.5">
                <span className="grid size-6 shrink-0 place-items-center rounded-full bg-muted font-mono text-[11px]">{index + 1}</span>
                <div className="min-w-0 flex-1"><strong className="block text-[13px]">{title}</strong><p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">{desc}</p></div>
                <span className="shrink-0">{icon}</span>
              </li>
            ))}
          </ol>
        </Panel>

        <Panel title="熔断与止损风险边界" description="风控事件需要价格与流动性配合，规则触发不等于成交保证" action={<ShieldAlert className="size-4" />}>
          <div className="grid gap-2">
            <div className="flex items-start gap-2.5 rounded-lg border border-warning/25 bg-warning/5 px-3 py-2.5">
              <Ban className="mt-0.5 size-4 shrink-0 text-warning" />
              <div><h3 className="text-[13px] font-semibold">事件熔断</h3><p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">指数或预设事件触发后，暂停相应新建仓动作并记录事件；是否恢复由后续规则判定。</p><div className="mt-1.5 flex gap-1.5"><Badge tone="amber">暂停新增风险</Badge><Badge>保留事件记录</Badge></div></div>
            </div>
            <div className="flex items-start gap-2.5 rounded-lg border border-destructive/25 bg-destructive/5 px-3 py-2.5">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
              <div><h3 className="text-[13px] font-semibold">无价格止损风险</h3><p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">没有可用价格、行情中断或流动性不足时，止损信号可能无法转化为有效委托与成交。</p><div className="mt-1.5 flex gap-1.5"><Badge tone="red">不保证止损价</Badge><Badge tone="red">不保证成交</Badge></div></div>
            </div>
          </div>
          <Alert className="mt-2 border-destructive/20 bg-destructive/5 text-destructive">
            <AlertOctagon />
            <AlertTitle>回测必须保留此类 warning</AlertTitle>
            <AlertDescription className="text-destructive/80">不得将理论止损价直接视为实际成交价。</AlertDescription>
          </Alert>
        </Panel>
      </div>

      <div className="flex flex-col items-center justify-between gap-2 border-t pt-3 text-[11px] text-muted-foreground sm:flex-row">
        <span className="font-mono tracking-wide">LayerLab Strategy Specification</span>
        <span>用户定稿 · 2026-11-11 · 做 T 可选，不占笔位</span>
      </div>
    </div>
  );
}
