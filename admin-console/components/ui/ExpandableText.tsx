"use client";

import { useState } from "react";
import { cn } from "@/components/ui/common";
import { ChevronDown, ChevronUp } from "lucide-react";

export const ExpandableText = ({ text, className, limit = 60 }: { text: string; className?: string; limit?: number }) => {
  const [expanded, setExpanded] = useState(false);

  if (!text) return <span className="text-gray-400">-</span>;
  if (text.length <= limit) return <div className={className}>{text}</div>;

  return (
    <div className={cn("relative group cursor-pointer", className)} onClick={() => setExpanded(!expanded)}>
      <div className={cn("break-words", expanded ? "" : "truncate")}>
        {expanded ? text : text.slice(0, limit) + "..."}
      </div>
      {!expanded && (
        <div className="absolute right-0 top-0 h-full w-8 bg-gradient-to-l from-white to-transparent md:hidden" />
      )}
      {expanded && (
         <div className="mt-1 text-xs text-blue-500 flex items-center hover:underline">
            <ChevronUp className="w-3 h-3 mr-1" /> Collapse
         </div>
      )}
    </div>
  );
};
