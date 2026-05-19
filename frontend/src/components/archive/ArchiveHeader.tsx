"use client";

import React from 'react';
import { ArrowLeft, Table as TableIcon, Map as MapIcon } from 'lucide-react';
import { Button } from "@/components/ui/button";
import { cn } from '@/lib/utils';

interface ArchiveHeaderProps {
  mode: 'bulk' | 'custom';
  onModeChange: (mode: 'bulk' | 'custom') => void;
  onBack: () => void;
}

const ArchiveHeader = ({ mode, onModeChange, onBack }: ArchiveHeaderProps) => {
  return (
    <header className="h-20 bg-white/80 backdrop-blur-xl border-b border-gray-100 sticky top-0 z-40 px-6 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={onBack} className="rounded-full hover:bg-gray-100">
          <ArrowLeft className="w-5 h-5 text-gray-700" />
        </Button>
        <img src="/assets/logo.svg" alt="OSeM Archive logo" className="h-8 w-8 shrink-0" />
        <h1 className="text-xl font-extrabold tracking-tight text-gray-900">OSeM Archive</h1>
      </div>
      
      <div className="bg-gray-100 p-1 rounded-full flex">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onModeChange('bulk')}
          className={cn(
            "rounded-full px-6 h-8 text-xs font-extrabold transition-all",
            mode === 'bulk'
              ? "bg-white text-blue-600 shadow-sm hover:bg-white"
              : "text-gray-500 hover:text-gray-700 hover:bg-transparent"
          )}
        >
          <TableIcon className="w-3.5 h-3.5 mr-2" /> Country / Region
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onModeChange('custom')}
          className={cn(
            "rounded-full px-6 h-8 text-xs font-extrabold transition-all",
            mode === 'custom'
              ? "bg-white text-blue-600 shadow-sm hover:bg-white"
              : "text-gray-500 hover:text-gray-700 hover:bg-transparent"
          )}
        >
          <MapIcon className="w-3.5 h-3.5 mr-2" /> Custom
        </Button>
      </div>
    </header>
  );
};

export default ArchiveHeader;