import { Slot } from "@radix-ui/react-slot";
import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost";
type Size = "sm" | "md";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  readonly variant?: Variant;
  readonly size?: Size;
  /** Render as the child element (Radix Slot pattern) — e.g. a Link. */
  readonly asChild?: boolean;
}

const VARIANT_CLASSES: Record<Variant, string> = {
  primary: "bg-accent text-white hover:opacity-90 disabled:opacity-50",
  secondary:
    "border border-border bg-bg-elevated text-text hover:border-accent disabled:opacity-50",
  ghost: "text-text-muted hover:text-text disabled:opacity-50",
};

const SIZE_CLASSES: Record<Size, string> = {
  sm: "h-8 px-3 text-sm",
  md: "h-9 px-4 text-sm",
};

export function Button({
  variant = "primary",
  size = "md",
  asChild = false,
  className = "",
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={`inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-accent ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...props}
    />
  );
}
