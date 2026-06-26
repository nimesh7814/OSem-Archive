"use client";

import * as React from "react";
import { format, parseISO } from "date-fns";
import { CalendarIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

interface DatePickerProps {
  /** ISO date string, e.g. "2024-06-03". */
  value?: string;
  /** Receives an ISO date string, or undefined when cleared. */
  onChange: (value: string | undefined) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  fromDate?: Date;
  toDate?: Date;
}

const parseValue = (value?: string) => {
  if (!value) return undefined;
  const parsed = parseISO(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
};

export function DatePicker({
  value,
  onChange,
  placeholder = "Select date",
  disabled,
  className,
  fromDate,
  toDate,
}: DatePickerProps) {
  const selected = parseValue(value);
  const disabledMatchers = [
    ...(fromDate ? [{ before: fromDate }] : []),
    ...(toDate ? [{ after: toDate }] : []),
  ];

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          disabled={disabled}
          className={cn(
            "w-full justify-start text-left font-medium",
            !selected && "text-muted-foreground",
            className,
          )}
        >
          <CalendarIcon className="h-4 w-4 shrink-0 opacity-60" />
          {selected ? format(selected, "yyyy/MM/dd") : placeholder}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-0" align="start">
        <Calendar
          mode="single"
          selected={selected}
          onSelect={(date) => onChange(date ? format(date, "yyyy-MM-dd") : undefined)}
          disabled={disabledMatchers.length ? disabledMatchers : undefined}
          defaultMonth={selected ?? toDate}
          autoFocus
        />
      </PopoverContent>
    </Popover>
  );
}
