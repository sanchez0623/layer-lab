import { useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"
import { Input } from "./input"
import { cn } from "cn"

/**
 * 可自由直接输入的数字输入框（替代原生 <input type="number"> 的上下箭头组件）。
 *
 * 原生 number 输入框的痛点：spinner 碍事、受控 value 为数字 + 逐键立即 Number() 回填，
 * 导致「清空即回 0 / 无法先清后输」，中文输入法下也常受限。本组件改为：
 *  - type="text" + inputMode="decimal"：像普通文本框一样自由敲数字（可空、可小数）。
 *  - 内部草稿态（draft）：输入过程中不立即回填外部 value，失焦 / 回车时才解析提交，
 *    避免清空即回弹、杜绝 NaN 泄漏。
 *  - 可选自定义 +/− 步进按钮（传入 step 时显示）取代原生箭头，点击按 step 加减并夹取。
 *  - 可选单位后缀（unit，如 % / 亿 / 日 / ‱）叠加在框内右侧。
 *
 * 提交语义：
 *  - allowEmpty=false（默认）：空串或非法输入在失焦时回退到上一次有效值（不提交空）。
 *  - allowEmpty=true：空串提交 onChange('')（供「留空=沿用当前 / 置空」的字段）。
 *  - 合法输入提交 String(clamp(round(value))) 给 onChange。
 */
type NumberInputProps = Omit<
  React.ComponentProps<"input">,
  "onChange" | "value" | "type" | "inputMode"
> & {
  /** 受控值：number 或字符串（'' 表空）。 */
  value: string | number;
  /** 提交回调：只在失焦 / 回车 / 步进时触发，参数为可提交的字符串。 */
  onChange: (raw: string) => void;
  min?: number;
  max?: number;
  /** 传入时显示 +/− 步进按钮，并用于推断提交精度。 */
  step?: number;
  /** 提交时的保留小数位；缺省由 step 推断（0.1→1、0.5→1、0.01→2、整数 step→0）。 */
  precision?: number;
  /** 框内右侧单位后缀。 */
  unit?: string;
  /** true：空串提交 ''（留空语义）；false：空串回退上一次有效值。 */
  allowEmpty?: boolean;
};

const toText = (value: string | number): string =>
  value === "" || value === null || value === undefined ? "" : String(value);

const clampValue = (n: number, min?: number, max?: number): number => {
  if (min !== undefined && n < min) return min;
  if (max !== undefined && n > max) return max;
  return n;
};

const inferPrecision = (step?: number, precision?: number): number => {
  if (precision !== undefined) return precision;
  if (step !== undefined && !Number.isInteger(step)) {
    const frac = String(step).split(".")[1];
    return frac ? frac.length : 0;
  }
  // 整数 step（或未传 step）：不强制取整，保留用户输入的小数位（如 12.5%）。
  // 需要严格整数的字段（交易日、股数、金额等）请显式传 precision={0}。
  return 6;
};

const roundTo = (n: number, p: number): number => Number(n.toFixed(p));

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step,
  precision,
  unit,
  allowEmpty = false,
  className,
  disabled,
  placeholder,
  ...rest
}: NumberInputProps) {
  const [draft, setDraft] = useState<string>(() => toText(value));
  const focusedRef = useRef(false);
  const valueRef = useRef<typeof value>(value);
  valueRef.current = value;
  const p = inferPrecision(step, precision);

  // 外部 value 变化（未在编辑中）时同步草稿。
  useEffect(() => {
    if (!focusedRef.current) setDraft(toText(value));
  }, [value]);

  const lastValid = () => toText(valueRef.current);

  /** 解析并提交草稿；非法/空按 allowEmpty 决定提交空串或回退。 */
  const commit = (raw: string) => {
    const trimmed = raw.trim();
    if (trimmed === "") {
      if (allowEmpty) {
        setDraft("");
        onChange("");
        return;
      }
      setDraft(lastValid());
      return;
    }
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) {
      setDraft(lastValid());
      return;
    }
    const next = roundTo(clampValue(parsed, min, max), p);
    setDraft(String(next));
    onChange(String(next));
  };

  /** 步进：以当前草稿/值为基准加减 step，夹取并立即提交。 */
  const nudge = (dir: 1 | -1) => {
    const current = Number(draft);
    const base = Number.isFinite(current) ? current : Number(valueRef.current) || 0;
    const next = roundTo(clampValue(base + dir * (step ?? 1), min, max), p);
    setDraft(String(next));
    onChange(String(next));
  };

  const showStepper = step !== undefined && !disabled;

  return (
    <div className="relative">
      <Input
        type="text"
        inputMode="decimal"
        value={draft}
        disabled={disabled}
        placeholder={placeholder}
        onFocus={(event) => {
          focusedRef.current = true;
          setDraft(lastValid());
          event.currentTarget.select();
        }}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={(event) => {
          commit(event.currentTarget.value);
          focusedRef.current = false;
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            commit(event.currentTarget.value);
            event.currentTarget.blur();
          }
        }}
        className={cn(
          showStepper ? (unit ? "pr-10" : "pr-7") : unit ? "pr-8" : "",
          className
        )}
        {...rest}
      />
      {unit && (
        <span
          className={cn(
            "pointer-events-none absolute top-1/2 -translate-y-1/2 font-mono text-[11px] text-muted-foreground",
            showStepper ? "right-8" : "right-2"
          )}
        >
          {unit}
        </span>
      )}
      {showStepper && (
        <div className="absolute right-1 top-1/2 flex -translate-y-1/2 flex-col">
          <button
            type="button"
            tabIndex={-1}
            aria-label="增加"
            onClick={() => nudge(1)}
            className="grid size-4 place-items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ChevronUp className="size-3" />
          </button>
          <button
            type="button"
            tabIndex={-1}
            aria-label="减少"
            onClick={() => nudge(-1)}
            className="grid size-4 place-items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ChevronDown className="size-3" />
          </button>
        </div>
      )}
    </div>
  );
}

export default NumberInput;
