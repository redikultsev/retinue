// shadcn/ui Tabs' look (MIT) as plain buttons with ARIA roles; no Radix.
import type * as React from "react";
import { cn } from "@/lib/utils";

interface TabsListProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  tabs: { value: T; label: React.ReactNode }[];
  className?: string;
}

function TabsList<T extends string>({ value, onChange, tabs, className }: TabsListProps<T>) {
  return (
    <div role="tablist" className={cn("inline-flex h-10 items-center rounded-lg bg-muted p-1 text-muted-foreground", className)}>
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={tab.value === value}
          onClick={() => onChange(tab.value)}
          className={cn(
            "inline-flex h-full flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-3 text-sm font-medium transition-colors",
            tab.value === value ? "bg-background text-foreground shadow-sm" : "hover:text-foreground",
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export { TabsList };
