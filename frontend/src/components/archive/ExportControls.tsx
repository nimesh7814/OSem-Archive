"use client";

import React from 'react';
import { Download, Loader2 } from 'lucide-react';
import { motion } from 'framer-motion';
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

interface ExportControlsProps {
  onExport: (format: string) => void;
  disabled?: boolean;
  loading?: boolean;
}

const ExportControls = ({ onExport, disabled, loading }: ExportControlsProps) => {
  return (
    <div className="space-y-4">
      <Label className="text-xs font-bold text-gray-500 uppercase tracking-widest ml-1">Download Query</Label>
      <motion.div whileTap={{ scale: loading ? 1 : 0.98 }} whileHover={{ y: loading ? 0 : -1 }} className="relative">
        {loading && (
          <motion.span
            aria-hidden="true"
            className="absolute inset-0 rounded-2xl bg-blue-500/10"
            animate={{ opacity: [0.25, 0.55, 0.25] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
        )}
        <Button
          variant="outline"
          className={`relative w-full h-16 overflow-hidden rounded-2xl border-2 font-black transition-all flex items-center justify-center gap-3 ${loading ? 'border-blue-500 bg-blue-50 text-blue-800 shadow-lg shadow-blue-100' : 'border-blue-200 bg-blue-50/60 hover:border-blue-500 hover:bg-blue-50 text-blue-700'}`}
          onClick={() => onExport('download')}
          disabled={disabled || loading}
        >
          {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Download className="w-5 h-5" />}
          <span>{loading ? 'Preparing Download' : 'Download Data'}</span>
          {loading && <span className="absolute inset-x-0 bottom-0 h-1 bg-blue-400/40 animate-pulse" />}
        </Button>
      </motion.div>
    </div>
  );
};

export default ExportControls;