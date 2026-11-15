import { useEffect, useState } from 'react';
import {
  CalendarDays, Clock3, Database, FolderOpen, Info, Layers3, Radio, RefreshCw, Search, Server,
  ShieldCheck, Timer,
} from 'lucide-react';
import { toast } from 'sonner';
import { api } from '../lib/api';
import { cleanSymbolInput, dateTime, displaySymbol, integer, shortDate } from '../lib/format';
import { Badge, EmptyState, ErrorBanner, Field, FormGrid, Loading, PageHeader, Panel, SyntheticNotice } from '../components/Ui';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
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
    setFetching(true); setError('');
    try {
      const result = await api.fetchMarketData({
        symbol: code, start_date: startDate || undefined,
        end_date: endDate || undefined, include_intraday: includeIntraday,
      });
      await load();
      const minuteRows = result.intraday ? `，5分钟 ${integer(result.intraday.rows)} 行` : '';
      const resolved = [result.code || displaySymbol(result.symbol), result.board].filter(Boolean).join(' · ');
      toast.success(`已接入 ${result.name || resolved}（${resolved}）：日线 ${integer(result.daily.rows)} 行${minuteRows}，对应指数 ${result.index_symbol || ''}。`);
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

  const stats = [
    { icon: Database, tone: 'text-primary bg-accent', label: '已缓存数据集', value: integer(datasets.length), note: '来自真实行情源' },
    { icon: Layers3, tone: 'text-foreground bg-muted', label: '总数据行数', value: integer(totalRows), note: '日线 + 5 分钟' },
    { icon: Server, tone: 'text-warning bg-warning/10', label: '数据来源', value: integer(sourceCount), note: Array.from(new Set(datasets.map((item) => item.source))).join(' · ') || 'baostock · tencent' },
    { icon: Clock3, tone: 'text-success bg-success/10', label: '5分钟数据集', value: integer(minuteDatasets), note: `实时刷新间隔 ${intervalMinutes} 分钟` },
  ];

  return (
    <div className="animate-[page-in_.22s_ease-out]">
      <PageHeader
        eyebrow="MARKET / SOURCES"
        title="行情源中心"
        description="历史日线来自 quan_parquet / baostock，实时快照与 5 分钟 K 线直接接入腾讯行情；本地缓存后供回测与模拟盘复用。"
        actions={<Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}><RefreshCw className={loading ? 'animate-spin' : ''} />刷新</Button>}
      />

      <div className="mb-3 flex items-start gap-2.5 rounded-xl border border-primary/15 bg-accent/50 px-3.5 py-2.5 text-[13px] text-accent-foreground">
        <Radio className="mt-0.5 size-4 shrink-0" />
        <div>
          <strong className="block font-semibold">只读行情，不连接券商</strong>
          <span className="text-muted-foreground">系统仅向 baostock / 腾讯发起行情请求；本地撮合账户不会发送任何真实委托。</span>
        </div>
      </div>

      {error && <ErrorBanner message={error} onClose={() => setError('')} onRetry={() => void load()} />}

      <div className="mb-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
        {stats.map(({ icon: Icon, tone, label, value, note }) => (
          <Card key={label} className="flex-row items-center gap-2.5 px-3.5 py-3 shadow-xs">
            <span className={`grid size-10 shrink-0 place-items-center rounded-lg ${tone}`}><Icon className="size-5" /></span>
            <div className="min-w-0">
              <small className="block text-[11px] text-muted-foreground">{label}</small>
              <strong className="block font-mono text-xl font-medium text-foreground">{value}</strong>
              <span className="block truncate text-[11px] text-muted-foreground">{note}</span>
            </div>
          </Card>
        ))}
      </div>

      <div className="mb-2.5 grid gap-2.5 xl:grid-cols-2">
        <Panel title="接入行情数据" description="输入标的与区间，后端直接拉取并缓存" action={<Search className="size-4" />}>
          <FormGrid>
            <Field label="标的代码" hint="6 位数字，自动识别沪深">
              <Input value={symbol} onChange={(event) => setSymbol(cleanSymbolInput(event.target.value))} placeholder="例如 688200" inputMode="numeric" maxLength={6} />
            </Field>
            <Field label="标的名称" hint="自动识别">
              <Input value="由行情源返回" disabled />
            </Field>
            <Field label="开始日期">
              <div className="relative">
                <CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input type="date" value={startDate} max={endDate} onChange={(event) => setStartDate(event.target.value)} className="pl-8" />
              </div>
            </Field>
            <Field label="结束日期">
              <div className="relative">
                <CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input type="date" value={endDate} min={startDate} onChange={(event) => setEndDate(event.target.value)} className="pl-8" />
              </div>
            </Field>
            <label className="flex w-full items-center justify-between gap-4 rounded-lg border bg-card px-3 py-2.5 sm:w-[260px] sm:self-start">
              <span>
                <strong className="block text-[13px] font-medium text-foreground">同时接入腾讯 5 分钟</strong>
                <small className="mt-0.5 block text-[11px] text-muted-foreground">用于盘中路径与模拟盘</small>
              </span>
              <Switch checked={includeIntraday} onCheckedChange={setIncludeIntraday} />
            </label>
          </FormGrid>
          <Button className="mt-2 w-fit min-w-[220px]" onClick={() => void fetchMarket()} disabled={fetching}>
            {fetching ? <><RefreshCw className="animate-spin" />正在拉取行情</> : <><Search />接入 历史日线 + 腾讯</>}
          </Button>
          <div className="mt-2.5 flex items-start gap-2 rounded-lg bg-muted/60 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
            <ShieldCheck className="mt-0.5 size-3.5 shrink-0" />
            <span>日线使用 quan_parquet / baostock 前复权口径；5 分钟与实时快照来自腾讯。缓存文件保存在后端 data/cache 目录。</span>
          </div>
        </Panel>

        <Panel title="数据源职责" description="两个来源的分工、粒度与刷新频率" action={<Server className="size-4" />}>
          <div className="grid gap-2.5">
            <Card className="gap-2 px-3.5 py-3 shadow-none">
              <div className="flex items-center gap-2.5">
                <span className="grid size-8 place-items-center rounded-md bg-muted text-muted-foreground"><Database className="size-4" /></span>
                <div className="min-w-0 flex-1"><strong className="block text-[13px]">baostock</strong><small className="text-[11px] text-muted-foreground">历史日线</small></div>
                <Badge tone="green">已安装</Badge>
              </div>
              <p className="text-[12px] leading-5 text-muted-foreground">提供前复权 OHLCV、成交额、换手率与昨收，用于 16 笔序列、A3、大盘门与完整轮次回测。</p>
              <span className="text-[11px] text-muted-foreground">输出粒度：daily</span>
            </Card>
            <Card className="gap-2 px-3.5 py-3 shadow-none">
              <div className="flex items-center gap-2.5">
                <span className="grid size-8 place-items-center rounded-md bg-accent text-accent-foreground"><Radio className="size-4" /></span>
                <div className="min-w-0 flex-1"><strong className="block text-[13px]">腾讯行情</strong><small className="text-[11px] text-muted-foreground">实时快照 / 5 分钟</small></div>
                <Badge tone="indigo">已接入</Badge>
              </div>
              <p className="text-[12px] leading-5 text-muted-foreground">提供最新价、盘口一档、成交与换手，用于模拟盘撮合、止盈状态扫描与做 T 准入。</p>
              <span className="text-[11px] text-muted-foreground">刷新间隔：{intervalMinutes} 分钟</span>
            </Card>
          </div>
          <div className="mt-2.5 flex items-start gap-2 rounded-lg bg-muted/60 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
            <Timer className="mt-0.5 size-3.5 shrink-0" />
            <p>准实时扫描使用 5 分钟 K 线；信号确认使用连续 3 根 5 分钟 K 线，不再使用 1 分钟或 10 秒 tick。</p>
          </div>
          {sources?.note && (
            <div className="mt-2 flex items-start gap-2 text-[11px] leading-5 text-muted-foreground">
              <Info className="mt-0.5 size-3.5 shrink-0" /><span>{String(sources.note)}</span>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="缓存数据集" description="来源、覆盖区间与粒度均来自后端元数据" action={<Badge>{datasets.length} 个数据集</Badge>} flush>
        {loading ? <Loading label="正在读取行情缓存" /> : datasets.length === 0 ? (
          <EmptyState icon={<FolderOpen className="size-6" />} title="暂无行情缓存" description="输入标的代码并点击接入，后端会从历史数据源与腾讯拉取数据并落盘。" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>数据集</TableHead><TableHead>标的</TableHead><TableHead>粒度</TableHead><TableHead>数据来源</TableHead>
                <TableHead className="text-right">行数</TableHead><TableHead>覆盖区间</TableHead><TableHead>创建时间</TableHead><TableHead>属性</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {datasets.map((dataset) => (
                <TableRow key={dataset.id}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <span className="grid size-7 place-items-center rounded-md bg-muted text-muted-foreground"><Database className="size-3.5" /></span>
                      <div className="min-w-0">
                        <strong className="block text-[13px] font-medium">{dataset.name}</strong>
                        <small className="block font-mono text-[10px] text-muted-foreground">ID {dataset.id.slice(0, 12)}</small>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell><strong className="font-mono text-[12px]">{displaySymbol(dataset.symbol)}</strong></TableCell>
                  <TableCell><Badge tone="indigo">{timeframeLabels[dataset.timeframe] || dataset.timeframe}</Badge><small className="ml-1.5 font-mono text-[10px] text-muted-foreground">{dataset.timeframe}</small></TableCell>
                  <TableCell className="text-[12px]">{dataset.source}</TableCell>
                  <TableCell className="text-right font-mono text-[12px]">{integer(dataset.rows)}</TableCell>
                  <TableCell className="font-mono text-[11px]">{shortDate(dataset.start)} <span className="text-muted-foreground">→</span> {shortDate(dataset.end)}</TableCell>
                  <TableCell className="text-[12px]">{dateTime(dataset.created_at)}</TableCell>
                  <TableCell>{dataset.is_synthetic ? <SyntheticNotice compact /> : <Badge tone="green">真实源</Badge>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>
    </div>
  );
}


