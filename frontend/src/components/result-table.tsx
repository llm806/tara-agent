"use client";

import { ChevronDown, Database, Download } from "lucide-react";
import { type ReactNode, useState } from "react";

import { downloadCsv } from "@/lib/downloads";

type ResultTableProps = {
  result: Record<string, unknown>;
};

export function ResultTable({ result }: ResultTableProps) {
  const tables = findTables(result);
  if (tables.length === 0) {
    return null;
  }

  return (
    <>
      {tables.map((table) => (
        <ResultTableSection
          key={table.key}
          label={table.label}
          rows={table.rows}
        />
      ))}
    </>
  );
}

type ResultRows = {
  key: string;
  label: string;
  rows: Array<Record<string, unknown>>;
};

function ResultTableSection({ label, rows }: Omit<ResultRows, "key">) {
  const [open, setOpen] = useState(true);
  const columns = Object.keys(rows[0]).slice(0, 8);

  return (
    <details
      className="response-section result-panel"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <Database size={16} />
        <span>结构化结果</span>
        <small>{label} {rows.length} 行</small>
        <ChevronDown size={16} />
      </summary>
      <div className="section-body">
        <div className="artifact-toolbar">
          <button
            type="button"
            className="artifact-download"
            onClick={() => downloadCsv(rows, label)}
            aria-label={`下载${label}表格`}
          >
            <Download size={14} aria-hidden="true" />
            下载 CSV
          </button>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column}>{humanize(column)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={rowKey(row, index)}>
                  {columns.map((column) => (
                    <td key={column}>{formatValue(row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </details>
  );
}

const resultLists = [
  ["items", "样本"],
  ["asvs", "分类单元"],
  ["sample_occurrences", "样本出现记录"],
  ["observations", "观测记录"],
  ["groups", "分组摘要"],
  ["points", "关联数据点"],
] as const;

function findTables(result: Record<string, unknown>): ResultRows[] {
  const tables: ResultRows[] = [];
  const includedKeys = new Set<string>();
  for (const [key, label] of resultLists) {
    const value = result[key];
    if (Array.isArray(value) && value.length > 0 && value.every(isRecord)) {
      tables.push({ key, label, rows: value });
      includedKeys.add(key);
    }
  }
  for (const [key, value] of Object.entries(result)) {
    if (
      !includedKeys.has(key)
      && Array.isArray(value)
      && value.length > 0
      && value.every(isRecord)
    ) {
      tables.push({ key, label: humanize(key), rows: value });
    }
  }
  if (isRecord(result.sample)) {
    tables.push({ key: "sample", label: "样本信息", rows: [result.sample] });
  }
  return tables;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function rowKey(row: Record<string, unknown>, index: number): string {
  const key = row.sample_id ?? row.sample_id_pangaea ?? row.amplicon;
  return typeof key === "string" ? key : String(index);
}

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}

function formatValue(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return <span className="null-value">NA</span>;
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toPrecision(5);
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
