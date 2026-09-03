"use client";

import {
  cloneElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { useDialogFocus } from "@/lib/use-dialog-focus";

export function cx(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export interface AppShellProps {
  navigation?: ReactNode;
  header?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function AppShell({ navigation, header, children, className }: AppShellProps) {
  return (
    <div className={cx("cb-app-shell", className)}>
      {navigation ? <aside className="cb-app-shell__navigation">{navigation}</aside> : null}
      <main className="cb-app-shell__main">
        {header ? <div className="cb-app-shell__header">{header}</div> : null}
        <div className="cb-app-shell__content">{children}</div>
      </main>
    </div>
  );
}

export interface ProjectHeaderProps {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  headingLevel?: 1 | 2;
  className?: string;
}

export function ProjectHeader({
  eyebrow,
  title,
  description,
  meta,
  actions,
  headingLevel = 1,
  className,
}: ProjectHeaderProps) {
  return (
    <header className={cx("cb-project-header", className)}>
      <div className="cb-project-header__copy">
        {eyebrow ? <p className="cb-project-header__eyebrow">{eyebrow}</p> : null}
        {headingLevel === 1
          ? <h1 className="cb-project-header__title">{title}</h1>
          : <h2 className="cb-project-header__title">{title}</h2>}
        {description ? <p className="cb-project-header__description">{description}</p> : null}
        {meta ? <div className="cb-project-header__meta">{meta}</div> : null}
      </div>
      {actions ? <div className="cb-project-header__actions">{actions}</div> : null}
    </header>
  );
}

export interface StepNavigationItem<T extends string = string> {
  id: T;
  label: string;
  description?: string;
  disabled?: boolean;
  complete?: boolean;
}

export interface StepNavigationProps<T extends string = string> {
  steps: readonly StepNavigationItem<T>[];
  currentStep: T;
  onStepChange?: (step: T) => void;
  ariaLabel?: string;
  className?: string;
}

export function StepNavigation<T extends string>({
  steps,
  currentStep,
  onStepChange,
  ariaLabel = "Steg i processen",
  className,
}: StepNavigationProps<T>) {
  return (
    <nav className={cx("cb-step-navigation", className)} aria-label={ariaLabel}>
      <ol className="cb-step-navigation__list">
        {steps.map((step, index) => {
          const active = step.id === currentStep;
          return (
            <li
              key={step.id}
              className={cx(
                "cb-step-navigation__item",
                active && "is-active",
                step.complete && "is-complete",
              )}
            >
              <button
                type="button"
                className="cb-step-navigation__button"
                aria-current={active ? "step" : undefined}
                disabled={step.disabled || !onStepChange}
                onClick={() => onStepChange?.(step.id)}
              >
                <span className="cb-step-navigation__marker" aria-hidden="true">
                  {step.complete ? "✓" : index + 1}
                </span>
                <span className="cb-step-navigation__copy">
                  <strong>{step.label}</strong>
                  {step.description ? <small>{step.description}</small> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export interface ContextPanelProps {
  eyebrow?: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  headingLevel?: 2 | 3;
  className?: string;
}

export function ContextPanel({
  eyebrow,
  title,
  description,
  actions,
  footer,
  children,
  headingLevel = 2,
  className,
}: ContextPanelProps) {
  return (
    <section className={cx("cb-context-panel", className)}>
      {eyebrow || title || description || actions ? (
        <header className="cb-context-panel__header">
          <div>
            {eyebrow ? <p className="cb-context-panel__eyebrow">{eyebrow}</p> : null}
            {title
              ? headingLevel === 2
                ? <h2>{title}</h2>
                : <h3>{title}</h3>
              : null}
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="cb-context-panel__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="cb-context-panel__body">{children}</div>
      {footer ? <footer className="cb-context-panel__footer">{footer}</footer> : null}
    </section>
  );
}

export interface BottomActionBarProps {
  status?: ReactNode;
  secondary?: ReactNode;
  primary?: ReactNode;
  className?: string;
}

export function BottomActionBar({ status, secondary, primary, className }: BottomActionBarProps) {
  return (
    <footer className={cx("cb-bottom-action-bar", className)}>
      {status ? <div className="cb-bottom-action-bar__status" aria-live="polite">{status}</div> : <span />}
      <div className="cb-bottom-action-bar__actions">
        {secondary}
        {primary}
      </div>
    </footer>
  );
}

export interface PersistentModelCanvasProps {
  label?: string;
  toolbar?: ReactNode;
  status?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function PersistentModelCanvas({
  label = "Modellvy",
  toolbar,
  status,
  children,
  className,
}: PersistentModelCanvasProps) {
  return (
    <section className={cx("cb-model-canvas", className)} aria-label={label}>
      {toolbar ? <div className="cb-model-canvas__toolbar">{toolbar}</div> : null}
      <div className="cb-model-canvas__viewport">{children}</div>
      {status ? <div className="cb-model-canvas__status">{status}</div> : null}
    </section>
  );
}

export interface ComponentLibraryProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function ComponentLibrary({ title, description, actions, children, className }: ComponentLibraryProps) {
  const titleId = useId();
  return (
    <aside className={cx("cb-component-library", className)} aria-labelledby={titleId}>
      <header className="cb-component-library__header">
        <div>
          <h2 id={titleId}>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        {actions ? <div className="cb-component-library__actions">{actions}</div> : null}
      </header>
      <div className="cb-component-library__body">{children}</div>
    </aside>
  );
}

export interface PropertyInspectorProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function PropertyInspector({
  title,
  description,
  actions,
  footer,
  children,
  className,
}: PropertyInspectorProps) {
  const titleId = useId();
  return (
    <section className={cx("cb-property-inspector", className)} aria-labelledby={titleId}>
      <header className="cb-property-inspector__header">
        <div>
          <h2 id={titleId}>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        {actions ? <div className="cb-property-inspector__actions">{actions}</div> : null}
      </header>
      <div className="cb-property-inspector__body">{children}</div>
      {footer ? <footer className="cb-property-inspector__footer">{footer}</footer> : null}
    </section>
  );
}

export interface DimensionInputProps {
  label: string;
  value: number;
  unit?: string;
  min: number;
  max: number;
  step?: number;
  commitMode?: "clamp" | "reject";
  hint?: ReactNode;
  error?: ReactNode;
  disabled?: boolean;
  onPreview?: (value: number) => void;
  onCommit?: (value: number) => void;
  className?: string;
}

function clampAndSnap(value: number, min: number, max: number, step: number): number {
  const clamped = Math.min(max, Math.max(min, value));
  const snapped = min + Math.round((clamped - min) / step) * step;
  return Math.min(max, Math.max(min, Number(snapped.toFixed(8))));
}

interface ExactDecimal {
  coefficient: bigint;
  scale: number;
}

function parseExactDecimal(raw: string): ExactDecimal | undefined {
  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > 128) return undefined;
  const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/.exec(trimmed);
  if (!match) return undefined;

  const fraction = match[3] ?? match[4] ?? "";
  const exponent = Number(match[5] ?? "0");
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) return undefined;

  const unsigned = BigInt(`${match[2] ?? "0"}${fraction}` || "0");
  let coefficient = match[1] === "-" ? -unsigned : unsigned;
  let scale = fraction.length - exponent;
  if (scale < 0) {
    coefficient *= 10n ** BigInt(-scale);
    scale = 0;
  }
  return { coefficient, scale };
}

function followsExactDecimalStep(raw: string, min: number, step: number): boolean {
  const value = parseExactDecimal(raw);
  const origin = parseExactDecimal(String(min));
  const increment = parseExactDecimal(String(step));
  if (!value || !origin || !increment || increment.coefficient <= 0n) return false;

  const scale = Math.max(value.scale, origin.scale, increment.scale);
  const scaled = (decimal: ExactDecimal) => (
    decimal.coefficient * 10n ** BigInt(scale - decimal.scale)
  );
  return (scaled(value) - scaled(origin)) % scaled(increment) === 0n;
}

export function DimensionInput({
  label,
  value,
  unit = "mm",
  min,
  max,
  step = 1,
  commitMode = "clamp",
  hint,
  error,
  disabled,
  onPreview,
  onCommit,
  className,
}: DimensionInputProps) {
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState(String(value));
  const [validationError, setValidationError] = useState<string>();

  useEffect(() => {
    if (document.activeElement !== inputRef.current) {
      setDraft(String(value));
      setValidationError(undefined);
    }
  }, [value]);

  const rejectedValueMessage = (raw: string): string | undefined => {
    const parsed = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(parsed)) return `Ange ${label.toLocaleLowerCase("sv-SE")} som ett tal.`;
    if (parsed < min || parsed > max) {
      return `Ange ett värde mellan ${min.toLocaleString("sv-SE")} och ${max.toLocaleString("sv-SE")} ${unit}.`;
    }
    if (!followsExactDecimalStep(raw, min, step)) {
      return `Ange värdet i steg om ${step.toLocaleString("sv-SE")} ${unit}.`;
    }
    return undefined;
  };

  const commit = () => {
    const parsed = Number(draft);
    if (commitMode === "reject") {
      const nextError = rejectedValueMessage(draft);
      setValidationError(nextError);
      if (nextError) return;
      setDraft(String(parsed));
      onCommit?.(parsed);
      return;
    }
    if (!Number.isFinite(parsed) || draft.trim() === "") {
      setDraft(String(value));
      return;
    }
    const next = clampAndSnap(parsed, min, max, step);
    setDraft(String(next));
    onCommit?.(next);
  };

  const renderedError = error ?? validationError;
  const describedBy = [hint ? hintId : undefined, renderedError ? errorId : undefined].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("cb-dimension-input", Boolean(renderedError) && "has-error", className)}>
      <label htmlFor={inputId}>{label}</label>
      <div className="cb-dimension-input__control">
        <input
          ref={inputRef}
          id={inputId}
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          value={draft}
          disabled={disabled}
          aria-invalid={Boolean(renderedError)}
          aria-describedby={describedBy}
          onChange={(event) => {
            const nextDraft = event.target.value;
            setDraft(nextDraft);
            if (commitMode === "reject") setValidationError(rejectedValueMessage(nextDraft));
            const parsed = Number(nextDraft);
            if (nextDraft.trim() !== "" && Number.isFinite(parsed)) onPreview?.(parsed);
          }}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setDraft(String(value));
              setValidationError(undefined);
              event.currentTarget.blur();
            }
          }}
        />
        <span aria-hidden="true">{unit}</span>
      </div>
      {hint ? <small id={hintId} className="cb-dimension-input__hint">{hint}</small> : null}
      {renderedError ? <small id={errorId} className="cb-dimension-input__error">{renderedError}</small> : null}
    </div>
  );
}

export interface DimensionHandleProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  label: string;
  axis?: "x" | "y" | "z";
  keyboardStep?: number;
  onNudge?: (delta: number) => void;
  children?: ReactNode;
}

export function DimensionHandle({
  label,
  axis,
  keyboardStep = 10,
  onNudge,
  children,
  className,
  onKeyDown,
  ...buttonProps
}: DimensionHandleProps) {
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    onKeyDown?.(event);
    if (event.defaultPrevented || !onNudge) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
      event.preventDefault();
      onNudge(-keyboardStep);
    }
    if (event.key === "ArrowRight" || event.key === "ArrowUp") {
      event.preventDefault();
      onNudge(keyboardStep);
    }
  };
  return (
    <button
      {...buttonProps}
      type={buttonProps.type ?? "button"}
      className={cx("cb-dimension-handle", className)}
      aria-label={label}
      data-axis={axis}
      onKeyDown={handleKeyDown}
    >
      {children ?? <span aria-hidden="true">↔</span>}
    </button>
  );
}

export interface SegmentedControlOption<T extends string> {
  id: T;
  label: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
}

export interface SegmentedControlProps<T extends string> {
  label: string;
  value: T;
  options: readonly SegmentedControlOption<T>[];
  onChange: (value: T) => void;
  className?: string;
}

export function SegmentedControl<T extends string>({ label, value, options, onChange, className }: SegmentedControlProps<T>) {
  const name = useId();
  return (
    <fieldset className={cx("cb-segmented-control", className)}>
      <legend>{label}</legend>
      <div className="cb-segmented-control__options">
        {options.map((option) => (
          <label key={option.id} className={option.id === value ? "is-selected" : undefined}>
            <input
              type="radio"
              name={name}
              value={option.id}
              checked={option.id === value}
              disabled={option.disabled}
              onChange={() => onChange(option.id)}
            />
            <span>
              <strong>{option.label}</strong>
              {option.description ? <small>{option.description}</small> : null}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export interface SelectableCardProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title"> {
  selected: boolean;
  title: ReactNode;
  description?: ReactNode;
  media?: ReactNode;
  meta?: ReactNode;
}

export function SelectableCard({
  selected,
  title,
  description,
  media,
  meta,
  className,
  type = "button",
  ...buttonProps
}: SelectableCardProps) {
  return (
    <button
      {...buttonProps}
      type={type}
      className={cx("cb-selectable-card", selected && "is-selected", className)}
      aria-pressed={selected}
    >
      {media ? <span className="cb-selectable-card__media">{media}</span> : null}
      <span className="cb-selectable-card__copy">
        <strong>{title}</strong>
        {description ? <small>{description}</small> : null}
      </span>
      {meta ? <span className="cb-selectable-card__meta">{meta}</span> : null}
    </button>
  );
}

export type StatusTone = "neutral" | "approved" | "decision" | "blocked" | "info";

export interface StatusRowProps {
  status: StatusTone;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  live?: boolean;
  className?: string;
}

export function StatusRow({ status, title, description, icon, actions, live, className }: StatusRowProps) {
  return (
    <div
      className={cx("cb-status-row", `cb-status-${status}`, className)}
      data-status={status}
      role={live ? "status" : undefined}
      aria-live={live ? "polite" : undefined}
    >
      {icon ? <span className="cb-status-row__icon" aria-hidden="true">{icon}</span> : null}
      <span className="cb-status-row__copy">
        <strong>{title}</strong>
        {description ? <small>{description}</small> : null}
      </span>
      {actions ? <span className="cb-status-row__actions">{actions}</span> : null}
    </div>
  );
}

export interface ValidationItemProps {
  status: Exclude<StatusTone, "neutral" | "info">;
  title: ReactNode;
  summary?: ReactNode;
  details?: ReactNode;
  actions?: ReactNode;
  defaultOpen?: boolean;
  className?: string;
}

export function ValidationItem({
  status,
  title,
  summary,
  details,
  actions,
  defaultOpen,
  className,
}: ValidationItemProps) {
  const titleId = useId();
  return (
    <article className={cx("cb-validation-item", `cb-status-${status}`, className)} aria-labelledby={titleId}>
      <header className="cb-validation-item__header">
        <div>
          <h3 id={titleId}>{title}</h3>
          {summary ? <p>{summary}</p> : null}
        </div>
        <span className="cb-validation-item__label">
          {status === "approved" ? "Godkänt" : status === "decision" ? "Behöver beslut" : "Måste lösas"}
        </span>
      </header>
      {details ? (
        <details className="cb-validation-item__details" open={defaultOpen}>
          <summary>Visa detaljer</summary>
          <div>{details}</div>
        </details>
      ) : null}
      {actions ? <footer className="cb-validation-item__actions">{actions}</footer> : null}
    </article>
  );
}

export interface RevisionBadgeProps {
  revision: string | number;
  status?: "current" | "draft" | "historical";
  label?: string;
  className?: string;
}

export function RevisionBadge({ revision, status = "current", label, className }: RevisionBadgeProps) {
  const statusLabel = status === "current" ? "Aktuell" : status === "draft" ? "Utkast" : "Tidigare";
  return (
    <span className={cx("cb-revision-badge", `is-${status}`, className)}>
      <span>{label ?? `Version ${revision}`}</span>
      <small>{statusLabel}</small>
    </span>
  );
}

export interface StateProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export type EmptyStateProps = StateProps;

export function EmptyState({ title, description, icon, action, className }: EmptyStateProps) {
  return (
    <section className={cx("cb-state", "cb-empty-state", className)}>
      {icon ? <span className="cb-state__icon" aria-hidden="true">{icon}</span> : null}
      <h3>{title}</h3>
      {description ? <p>{description}</p> : null}
      {action ? <div className="cb-state__action">{action}</div> : null}
    </section>
  );
}

export interface LoadingStateProps extends Omit<StateProps, "action" | "icon"> {
  label?: string;
}

export function LoadingState({ title, description, label = "Laddar", className }: LoadingStateProps) {
  return (
    <section className={cx("cb-state", "cb-loading-state", className)} role="status" aria-live="polite">
      <span className="cb-loading-state__spinner" aria-hidden="true" />
      <h3>{title}</h3>
      {description ? <p>{description}</p> : null}
      <span className="cb-visually-hidden">{label}</span>
    </section>
  );
}

export type ErrorStateProps = StateProps;

export function ErrorState({ title, description, icon, action, className }: ErrorStateProps) {
  return (
    <section className={cx("cb-state", "cb-error-state", className)} role="alert">
      {icon ? <span className="cb-state__icon" aria-hidden="true">{icon}</span> : null}
      <h3>{title}</h3>
      {description ? <p>{description}</p> : null}
      {action ? <div className="cb-state__action">{action}</div> : null}
    </section>
  );
}

export interface ExportCardProps {
  title: ReactNode;
  description?: ReactNode;
  format?: ReactNode;
  meta?: ReactNode;
  status?: "ready" | "generating" | "error";
  preview?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function ExportCard({
  title,
  description,
  format,
  meta,
  status = "ready",
  preview,
  action,
  className,
}: ExportCardProps) {
  const titleId = useId();
  return (
    <article className={cx("cb-export-card", `is-${status}`, className)} aria-labelledby={titleId}>
      {preview ? <div className="cb-export-card__preview">{preview}</div> : null}
      <div className="cb-export-card__body">
        <header>
          <div>
            <h3 id={titleId}>{title}</h3>
            {description ? <p>{description}</p> : null}
          </div>
          {format ? <span className="cb-export-card__format">{format}</span> : null}
        </header>
        {meta ? <div className="cb-export-card__meta">{meta}</div> : null}
        <div className="cb-export-card__footer">
          <span className="cb-export-card__status">
            {status === "ready" ? "Klar att hämta" : status === "generating" ? "Skapas…" : "Kunde inte skapas"}
          </span>
          {action}
        </div>
      </div>
    </article>
  );
}

type DialogRole = "dialog" | "alertdialog";

interface DialogSurfaceProps {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  dismissible?: boolean;
  closeLabel?: string;
  role?: DialogRole;
  className?: string;
}

function DialogSurface({
  open,
  title,
  description,
  children,
  footer,
  onClose,
  dismissible = true,
  closeLabel = "Stäng",
  role = "dialog",
  className,
}: DialogSurfaceProps) {
  const surfaceRef = useRef<HTMLElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useDialogFocus(open, surfaceRef, onClose, dismissible);
  if (!open) return null;
  return (
    <div
      className="cb-overlay"
      data-modal-root="true"
      role="presentation"
      onMouseDown={(event) => {
        if (dismissible && event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={surfaceRef}
        tabIndex={-1}
        className={className}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
      >
        <header className="cb-dialog__header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p id={descriptionId}>{description}</p> : null}
          </div>
          {dismissible ? (
            <button type="button" className="cb-dialog__close" aria-label={closeLabel} onClick={onClose}>
              <span aria-hidden="true">×</span>
            </button>
          ) : null}
        </header>
        <div className="cb-dialog__body">{children}</div>
        {footer ? <footer className="cb-dialog__footer">{footer}</footer> : null}
      </section>
    </div>
  );
}

export interface ModalProps extends Omit<DialogSurfaceProps, "role" | "className"> {
  size?: "small" | "medium" | "large";
  className?: string;
}

export function Modal({ size = "medium", className, ...props }: ModalProps) {
  return <DialogSurface {...props} className={cx("cb-modal", `cb-modal--${size}`, className)} />;
}

export interface DrawerProps extends Omit<DialogSurfaceProps, "role" | "className"> {
  side?: "left" | "right";
  className?: string;
}

export function Drawer({ side = "right", className, ...props }: DrawerProps) {
  return <DialogSurface {...props} className={cx("cb-drawer", `cb-drawer--${side}`, className)} />;
}

export interface TooltipProps {
  content: ReactNode;
  children: ReactElement<{ "aria-describedby"?: string }>;
  placement?: "top" | "right" | "bottom" | "left";
  focusable?: boolean;
  className?: string;
}

export function Tooltip({ content, children, placement = "top", focusable, className }: TooltipProps) {
  const tooltipId = useId();
  const describedBy = [children.props["aria-describedby"], tooltipId].filter(Boolean).join(" ");
  const trigger = cloneElement(children, { "aria-describedby": describedBy });
  return (
    <span
      className={cx("cb-tooltip", `cb-tooltip--${placement}`, className)}
      tabIndex={focusable ? 0 : undefined}
      aria-describedby={focusable ? tooltipId : undefined}
    >
      {trigger}
      <span id={tooltipId} className="cb-tooltip__content" role="tooltip">{content}</span>
    </span>
  );
}

export interface ConfirmDialogProps {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  children,
  confirmLabel = "Bekräfta",
  cancelLabel = "Avbryt",
  tone = "default",
  busy,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  return (
    <DialogSurface
      open={open}
      title={title}
      description={description}
      onClose={onClose}
      dismissible={!busy}
      role="alertdialog"
      className="cb-confirm-dialog"
      footer={(
        <div className="cb-confirm-dialog__actions">
          <button type="button" disabled={busy} onClick={onClose}>{cancelLabel}</button>
          <button
            type="button"
            className={tone === "danger" ? "is-danger" : "is-primary"}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "Arbetar…" : confirmLabel}
          </button>
        </div>
      )}
    >
      {children ?? <span />}
    </DialogSurface>
  );
}
