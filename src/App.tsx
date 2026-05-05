import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Bar } from 'react-chartjs-2';
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  Tooltip,
} from 'chart.js';
import type { ChartOptions } from 'chart.js';

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend, BarController);

type SortDirection = 'asc' | 'desc';
type RangeSelection = 'week' | 'month' | 'prev-month' | '7d' | '30d' | '90d' | 'all';
type TzMode = 'local' | 'utc';
type ModelSortCol = 'turns' | 'input' | 'output' | 'cache_read' | 'cache_creation' | 'cost';
type SessionSortCol = 'last' | 'duration_min' | 'turns' | 'input' | 'output' | 'cost';
type ProjectSortCol = 'sessions' | 'turns' | 'input' | 'output' | 'cost';
type BranchSortCol = 'sessions' | 'turns' | 'input' | 'output' | 'cost';

interface DashboardRecord {
  day: string;
  model: string;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  turns: number;
}

interface HourlyRecord {
  day: string;
  hour: number;
  model: string;
  output: number;
  turns: number;
}

interface SessionRecord {
  session_id: string;
  session_label: string;
  project: string;
  branch: string;
  last: string;
  last_date: string;
  duration_min: number;
  model: string;
  turns: number;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
}

interface ProjectUsageRow {
  day: string;
  model: string;
  project: string;
  branch: string;
  session_id: string;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  turns: number;
}

interface DashboardPayload {
  all_models: string[];
  daily_by_model: DashboardRecord[];
  hourly_by_model: HourlyRecord[];
  project_usage_by_day: ProjectUsageRow[];
  sessions_all: SessionRecord[];
  generated_at: string;
  error?: string;
}

interface DailyAggregate extends DashboardRecord {
  cost: number;
}

interface AggregatedByModel {
  model: string;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  turns: number;
  sessions: number;
  cost: number;
}

interface AggregatedProject {
  project: string;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  turns: number;
  sessions: number;
  cost: number;
}

interface AggregatedProjectBranch {
  project: string;
  branch: string;
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  turns: number;
  sessions: number;
  cost: number;
}

interface HourlyPressureBucket {
  hour: number;
  avgTurns: number;
  avgOutput: number;
  totalTurns: number;
  peak: boolean;
}

interface ProjectHeatmapRow {
  project: string;
  total: number;
  cells: number[];
}

interface DashboardTotals {
  daily: DailyAggregate[];
  byModel: AggregatedByModel[];
  filteredSessions: SessionRecord[];
  byProject: AggregatedProject[];
  byProjectBranch: AggregatedProjectBranch[];
  hourlyByHour: HourlyPressureBucket[];
  dayCount: number;
  heatmapDays: string[];
  heatmapRows: ProjectHeatmapRow[];
  heatmapMax: number;
  sessions: {
    count: number;
    turns: number;
    input: number;
    output: number;
    cacheRead: number;
    cacheCreation: number;
    cost: number;
    tokenVolume: number;
    dailyAverageCost: number;
    costPerMillionTokens: number;
    cacheShare: number;
    outputInputRatio: number;
    latestActivity: string;
    surgeDays: number;
  };
}

type SortState<TCol extends string> = {
  col: TCol;
  dir: SortDirection;
};

type PricingRate = {
  input: number;
  output: number;
  cache_write: number;
  cache_read: number;
};

const TOKEN_COLORS = {
  input: 'rgba(79, 137, 255, 0.88)',
  output: 'rgba(169, 128, 255, 0.86)',
  cache_read: 'rgba(65, 211, 124, 0.76)',
  cache_creation: 'rgba(232, 169, 43, 0.78)',
};

const RANGE_LABELS: Record<RangeSelection, string> = {
  week: 'This Week',
  month: 'This Month',
  'prev-month': 'Previous Month',
  '7d': 'Last 7 Days',
  '30d': 'Last 30 Days',
  '90d': 'Last 90 Days',
  all: 'All Time',
};

const RANGE_TICKS: Record<RangeSelection, number> = {
  week: 7,
  month: 15,
  'prev-month': 15,
  '7d': 7,
  '30d': 15,
  '90d': 13,
  all: 12,
};

const VALID_RANGES: RangeSelection[] = ['week', 'month', 'prev-month', '7d', '30d', '90d', 'all'];
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

const PRICING: Record<string, PricingRate> = {
  'claude-opus-4-7': { input: 5.0, output: 25.0, cache_write: 6.25, cache_read: 0.5 },
  'claude-opus-4-6': { input: 5.0, output: 25.0, cache_write: 6.25, cache_read: 0.5 },
  'claude-opus-4-5': { input: 5.0, output: 25.0, cache_write: 6.25, cache_read: 0.5 },
  'claude-sonnet-4-7': { input: 3.0, output: 15.0, cache_write: 3.75, cache_read: 0.3 },
  'claude-sonnet-4-6': { input: 3.0, output: 15.0, cache_write: 3.75, cache_read: 0.3 },
  'claude-sonnet-4-5': { input: 3.0, output: 15.0, cache_write: 3.75, cache_read: 0.3 },
  'claude-haiku-4-7': { input: 1.0, output: 5.0, cache_write: 1.25, cache_read: 0.1 },
  'claude-haiku-4-6': { input: 1.0, output: 5.0, cache_write: 1.25, cache_read: 0.1 },
  'claude-haiku-4-5': { input: 1.0, output: 5.0, cache_write: 1.25, cache_read: 0.1 },
};

const initialSessionSort: SortState<SessionSortCol> = { col: 'last', dir: 'desc' };
const initialModelSort: SortState<ModelSortCol> = { col: 'cost', dir: 'desc' };
const initialProjectSort: SortState<ProjectSortCol> = { col: 'cost', dir: 'desc' };
const initialBranchSort: SortState<BranchSortCol> = { col: 'cost', dir: 'desc' };

function isBillable(model: string): boolean {
  const lowered = model.toLowerCase();
  return lowered.includes('opus') || lowered.includes('sonnet') || lowered.includes('haiku');
}

function getPricing(model: string): PricingRate | null {
  if (PRICING[model]) {
    return PRICING[model];
  }

  for (const knownModel of Object.keys(PRICING)) {
    if (model.startsWith(knownModel)) {
      return PRICING[knownModel];
    }
  }

  const m = model.toLowerCase();
  if (m.includes('opus')) return PRICING['claude-opus-4-7'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku')) return PRICING['claude-haiku-4-5'];
  return null;
}

function costForModel(model: string, input: number, output: number, cacheRead: number, cacheCreation: number): number {
  if (!isBillable(model)) {
    return 0;
  }

  const pricing = getPricing(model);
  if (!pricing) {
    return 0;
  }

  return (
    (input * pricing.input) / 1_000_000 +
    (output * pricing.output) / 1_000_000 +
    (cacheRead * pricing.cache_read) / 1_000_000 +
    (cacheCreation * pricing.cache_write) / 1_000_000
  );
}

function formatTokens(value: number): string {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return Math.round(value).toLocaleString();
}

function formatCost(value: number): string {
  return `$${value.toFixed(4)}`;
}

function formatCostBig(value: number): string {
  return `$${value.toFixed(2)}`;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return '0.0%';
  return `${(value * 100).toFixed(1)}%`;
}

function formatDuration(minutes: number): string {
  return `${Number.isFinite(minutes) ? minutes.toFixed(1) : '0.0'}m`;
}

function modelPriority(model: string): number {
  const lowered = model.toLowerCase();
  if (lowered.includes('opus')) return 0;
  if (lowered.includes('sonnet')) return 1;
  if (lowered.includes('haiku')) return 2;
  return 3;
}

function compactDate(day: string): string {
  if (!day || day.length < 10) return day || 'n/a';
  const date = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return day;
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(date);
}

function readRangeFromURL(): RangeSelection {
  const params = new URLSearchParams(window.location.search);
  const range = params.get('range');
  return range && VALID_RANGES.includes(range as RangeSelection) ? (range as RangeSelection) : '30d';
}

function parseModelSelectionFromURL(allModels: string[]): Set<string> {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('models');
  if (!raw) {
    return new Set(allModels.filter(isBillable));
  }

  const requested = new Set(raw.split(',').map((candidate) => candidate.trim()).filter(Boolean));
  return new Set(allModels.filter((model) => requested.has(model)));
}

function isDefaultModelSelection(allModels: string[], models: Set<string>): boolean {
  const billable = allModels.filter(isBillable);
  if (billable.length !== models.size) {
    return false;
  }
  return billable.every((model) => models.has(model));
}

function readRangeBounds(range: RangeSelection): { start: string | null; end: string | null } {
  if (range === 'all') {
    return { start: null, end: null };
  }

  const today = new Date();
  const toIso = (value: Date): string => value.toISOString().slice(0, 10);

  if (range === 'week') {
    const day = today.getDay();
    const diffToMonday = day === 0 ? 6 : day - 1;
    const monday = new Date(today);
    monday.setDate(today.getDate() - diffToMonday);
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    return { start: toIso(monday), end: toIso(sunday) };
  }

  if (range === 'month') {
    return {
      start: toIso(new Date(today.getFullYear(), today.getMonth(), 1)),
      end: toIso(new Date(today.getFullYear(), today.getMonth() + 1, 0)),
    };
  }

  if (range === 'prev-month') {
    return {
      start: toIso(new Date(today.getFullYear(), today.getMonth() - 1, 1)),
      end: toIso(new Date(today.getFullYear(), today.getMonth(), 0)),
    };
  }

  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const start = new Date();
  start.setDate(start.getDate() - days);
  return { start: toIso(start), end: null };
}

function rangeIncludesToday(range: RangeSelection): boolean {
  if (range === 'all') {
    return true;
  }

  const { start, end } = readRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function localOffsetHours(): number {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

function utcHourToDisplayHour(hour: number, mode: TzMode): number {
  if (mode === 'utc') return hour;
  return (((hour + localOffsetHours()) % 24) + 24) % 24;
}

function formatHour(hour: number): string {
  return `${String(hour).padStart(2, '0')}:00`;
}

function isPeakHour(displayHour: number, tz: TzMode): boolean {
  if (tz === 'utc') return PEAK_HOURS_UTC.has(displayHour);
  return PEAK_HOURS_UTC.has((displayHour - localOffsetHours() + 24) % 24);
}

function tzDisplayName(tz: TzMode): string {
  if (tz === 'utc') return 'UTC';
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
}

function csvField(value: string | number): string {
  const raw = `${value}`;
  if (raw.includes('"') || raw.includes(',') || raw.includes('\n')) {
    return `"${raw.replace(/"/g, '""')}"`;
  }
  return raw;
}

function downloadCSV(report: string, headers: string[], rows: Array<(string | number)[]>): void {
  const lines = [headers.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  link.href = URL.createObjectURL(blob);
  link.download = `${report}_${timestamp}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function sortableValue(value: unknown): number | string {
  return typeof value === 'number' || typeof value === 'string' ? value : 0;
}

function numericSort<T>(rows: T[], col: keyof T, dir: SortDirection): T[] {
  return [...rows].sort((a, b) => {
    const left = sortableValue(a[col]);
    const right = sortableValue(b[col]);
    if (left < right) return dir === 'desc' ? 1 : -1;
    if (left > right) return dir === 'desc' ? -1 : 1;
    return 0;
  });
}

function useDashboardTotals(
  payload: DashboardPayload | null,
  selectedModels: Set<string>,
  selectedRange: RangeSelection,
  hourlyTz: TzMode,
): DashboardTotals | null {
  return useMemo(() => {
    if (!payload) return null;

    const { start, end } = readRangeBounds(selectedRange);
    const inRange = (day: string) => (!start || day >= start) && (!end || day <= end);
    const modelSelected = (model: string) => selectedModels.has(model);

    const filteredDaily = payload.daily_by_model.filter((row) => modelSelected(row.model) && inRange(row.day));
    const filteredSessions = payload.sessions_all.filter((session) => modelSelected(session.model) && inRange(session.last_date));
    const filteredProjectUsage = payload.project_usage_by_day.filter((row) => modelSelected(row.model) && inRange(row.day));

    const dailyByDay = new Map<string, DailyAggregate>();
    for (const row of filteredDaily) {
      const rowCost = costForModel(row.model, row.input, row.output, row.cache_read, row.cache_creation);
      const existing = dailyByDay.get(row.day);
      if (!existing) {
        dailyByDay.set(row.day, { ...row, model: 'all', cost: rowCost });
        continue;
      }
      existing.input += row.input;
      existing.output += row.output;
      existing.cache_read += row.cache_read;
      existing.cache_creation += row.cache_creation;
      existing.turns += row.turns;
      existing.cost += rowCost;
    }
    const daily = [...dailyByDay.values()].sort((a, b) => a.day.localeCompare(b.day));

    const byModel = new Map<string, AggregatedByModel>();
    for (const row of filteredDaily) {
      const rowCost = costForModel(row.model, row.input, row.output, row.cache_read, row.cache_creation);
      const existing = byModel.get(row.model);
      if (!existing) {
        byModel.set(row.model, {
          model: row.model,
          input: row.input,
          output: row.output,
          cache_read: row.cache_read,
          cache_creation: row.cache_creation,
          turns: row.turns,
          sessions: 0,
          cost: rowCost,
        });
        continue;
      }
      existing.input += row.input;
      existing.output += row.output;
      existing.cache_read += row.cache_read;
      existing.cache_creation += row.cache_creation;
      existing.turns += row.turns;
      existing.cost += rowCost;
    }

    for (const session of filteredSessions) {
      const current = byModel.get(session.model);
      if (current) {
        current.sessions += 1;
      }
    }

    const modelRows = [...byModel.values()].sort((a, b) => b.cost - a.cost);

    const projectBuckets = new Map<string, AggregatedProject & { sessionIds: Set<string> }>();
    const heatmapCost = new Map<string, Map<string, number>>();
    for (const row of filteredProjectUsage) {
      const cost = costForModel(row.model, row.input, row.output, row.cache_read, row.cache_creation);
      const aggregate = projectBuckets.get(row.project);
      if (!aggregate) {
        projectBuckets.set(row.project, {
          project: row.project,
          input: row.input,
          output: row.output,
          cache_read: row.cache_read,
          cache_creation: row.cache_creation,
          turns: row.turns,
          sessions: 0,
          cost,
          sessionIds: new Set(row.session_id ? [row.session_id] : []),
        });
      } else {
        aggregate.input += row.input;
        aggregate.output += row.output;
        aggregate.cache_read += row.cache_read;
        aggregate.cache_creation += row.cache_creation;
        aggregate.turns += row.turns;
        aggregate.cost += cost;
        if (row.session_id) aggregate.sessionIds.add(row.session_id);
      }

      const byDay = heatmapCost.get(row.project) ?? new Map<string, number>();
      byDay.set(row.day, (byDay.get(row.day) ?? 0) + cost);
      heatmapCost.set(row.project, byDay);
    }

    const byProject = [...projectBuckets.values()]
      .map((bucket) => {
        const sessions = bucket.sessionIds.size;
        const { sessionIds, ...project } = bucket;
        return { ...project, sessions };
      })
      .sort((a, b) => b.cost - a.cost);

    const branchBuckets = new Map<string, AggregatedProjectBranch & { sessionIds: Set<string> }>();
    for (const row of filteredProjectUsage) {
      const branch = row.branch || '';
      const key = `${row.project}\u001f${branch}`;
      const cost = costForModel(row.model, row.input, row.output, row.cache_read, row.cache_creation);
      const aggregate = branchBuckets.get(key);
      if (!aggregate) {
        branchBuckets.set(key, {
          project: row.project,
          branch,
          input: row.input,
          output: row.output,
          cache_read: row.cache_read,
          cache_creation: row.cache_creation,
          turns: row.turns,
          sessions: 0,
          cost,
          sessionIds: new Set(row.session_id ? [row.session_id] : []),
        });
        continue;
      }
      aggregate.input += row.input;
      aggregate.output += row.output;
      aggregate.cache_read += row.cache_read;
      aggregate.cache_creation += row.cache_creation;
      aggregate.turns += row.turns;
      aggregate.cost += cost;
      if (row.session_id) aggregate.sessionIds.add(row.session_id);
    }

    const byProjectBranch = [...branchBuckets.values()].map((bucket) => {
      const sessions = bucket.sessionIds.size;
      const { sessionIds, ...projectBranch } = bucket;
      return { ...projectBranch, sessions };
    });

    const filteredHourly = payload.hourly_by_model.filter((row) => modelSelected(row.model) && inRange(row.day));
    const hourlyByHour = Array.from({ length: 24 }, (_, hour): HourlyPressureBucket => ({
      hour,
      avgTurns: 0,
      avgOutput: 0,
      totalTurns: 0,
      peak: isPeakHour(hour, hourlyTz),
    }));
    const activeDays = new Set<string>();
    for (const row of filteredHourly) {
      const displayHour = utcHourToDisplayHour(row.hour, hourlyTz);
      const bucket = hourlyByHour[displayHour];
      bucket.avgTurns += row.turns || 0;
      bucket.avgOutput += row.output || 0;
      bucket.totalTurns += row.turns || 0;
      if (row.day) activeDays.add(row.day);
    }
    const dayCount = activeDays.size || daily.length || 1;
    for (const bucket of hourlyByHour) {
      bucket.avgTurns /= dayCount;
      bucket.avgOutput /= dayCount;
    }

    const heatmapDays = [...new Set(filteredProjectUsage.map((row) => row.day))].sort().slice(-8);
    const heatmapRows = byProject.slice(0, 6).map((project) => {
      const byDay = heatmapCost.get(project.project) ?? new Map<string, number>();
      const cells = heatmapDays.map((day) => byDay.get(day) ?? 0);
      return { project: project.project, total: project.cost, cells };
    });
    const heatmapMax = Math.max(1, ...heatmapRows.flatMap((row) => row.cells));

    const sessionCost = filteredSessions.reduce(
      (sum, session) => sum + costForModel(session.model, session.input, session.output, session.cache_read, session.cache_creation),
      0,
    );
    const totalInput = modelRows.reduce((sum, value) => sum + value.input, 0);
    const totalOutput = modelRows.reduce((sum, value) => sum + value.output, 0);
    const totalCacheRead = modelRows.reduce((sum, value) => sum + value.cache_read, 0);
    const totalCacheCreation = modelRows.reduce((sum, value) => sum + value.cache_creation, 0);
    const totalTurns = modelRows.reduce((sum, value) => sum + value.turns, 0);
    const tokenVolume = totalInput + totalOutput + totalCacheRead + totalCacheCreation;
    const averageDailyCost = daily.length ? sessionCost / daily.length : 0;
    const averageDayCost = daily.length ? daily.reduce((sum, day) => sum + day.cost, 0) / daily.length : 0;
    const surgeDays = averageDayCost > 0 ? daily.filter((day) => day.cost > averageDayCost * 1.75).length : 0;
    const latestActivity = filteredSessions.reduce((latest, session) => (session.last > latest ? session.last : latest), '');

    return {
      daily,
      byModel: modelRows,
      filteredSessions,
      byProject,
      byProjectBranch,
      hourlyByHour,
      dayCount,
      heatmapDays,
      heatmapRows,
      heatmapMax,
      sessions: {
        count: filteredSessions.length,
        turns: totalTurns,
        input: totalInput,
        output: totalOutput,
        cacheRead: totalCacheRead,
        cacheCreation: totalCacheCreation,
        cost: sessionCost,
        tokenVolume,
        dailyAverageCost: averageDailyCost,
        costPerMillionTokens: tokenVolume > 0 ? sessionCost / (tokenVolume / 1_000_000) : 0,
        cacheShare: totalInput + totalCacheRead + totalCacheCreation > 0
          ? totalCacheRead / (totalInput + totalCacheRead + totalCacheCreation)
          : 0,
        outputInputRatio: totalInput > 0 ? totalOutput / totalInput : 0,
        latestActivity,
        surgeDays,
      },
    };
  }, [hourlyTz, payload, selectedModels, selectedRange]);
}

function StatusDot({ tone = 'good' }: { tone?: 'good' | 'warn' | 'danger' }) {
  return <span className={`status-dot ${tone}`} aria-hidden="true" />;
}

function ShortMetric({ label, value, note, tone }: { label: string; value: string; note?: string; tone?: 'good' | 'warn' }) {
  return (
    <div className={`short-metric ${tone ?? ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </div>
  );
}

function RankedModelMix({ rows, totalCost }: { rows: AggregatedByModel[]; totalCost: number }) {
  if (!rows.length) {
    return <div className="empty-state">No model traffic in the current scope.</div>;
  }

  const max = Math.max(1, ...rows.map((row) => row.cost));
  return (
    <div className="ranked-list">
      {rows.slice(0, 8).map((row, index) => {
        const share = totalCost > 0 ? row.cost / totalCost : 0;
        return (
          <div className="ranked-row" key={row.model}>
            <div className="ranked-topline">
              <span className="rank">{index + 1}</span>
              <span className="ranked-name">{row.model}</span>
              <strong>{isBillable(row.model) ? formatCost(row.cost) : 'n/a'}</strong>
            </div>
            <div className="mix-track" aria-hidden="true">
              <span style={{ width: `${Math.max(4, (row.cost / max) * 100)}%` }} />
            </div>
            <div className="ranked-meta">
              <span>{formatPercent(share)} of cost</span>
              <span>{formatTokens(row.input + row.output)} prompt/output</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function HourlyPressure({ buckets, tz }: { buckets: HourlyPressureBucket[]; tz: TzMode }) {
  const maxTurns = Math.max(1, ...buckets.map((bucket) => bucket.avgTurns));
  return (
    <div className="hour-grid" role="list" aria-label={`Hourly pressure in ${tzDisplayName(tz)}`}>
      {buckets.map((bucket) => {
        const intensity = Math.min(1, bucket.avgTurns / maxTurns);
        return (
          <div
            className={`hour-cell ${bucket.peak ? 'peak' : ''}`}
            key={bucket.hour}
            style={{ '--pressure': intensity } as CSSProperties}
            title={`${formatHour(bucket.hour)} - ${bucket.avgTurns.toFixed(2)} avg turns - ${formatTokens(bucket.avgOutput)} avg output`}
            role="listitem"
          >
            <span>{bucket.hour % 3 === 0 ? String(bucket.hour).padStart(2, '0') : ''}</span>
          </div>
        );
      })}
    </div>
  );
}

function ProjectHeatmap({ days, rows, max }: { days: string[]; rows: ProjectHeatmapRow[]; max: number }) {
  if (!days.length || !rows.length) {
    return <div className="empty-state">No project spend in the current scope.</div>;
  }

  return (
    <div className="heatmap">
      <div className="heatmap-head">
        <span>Project</span>
        {days.map((day) => <span key={day}>{compactDate(day)}</span>)}
        <span>Total</span>
      </div>
      {rows.map((row) => (
        <div className="heatmap-row" key={row.project}>
          <span className="heatmap-project" title={row.project}>{row.project}</span>
          {row.cells.map((value, index) => (
            <span
              className="heatmap-cell"
              key={`${row.project}-${days[index]}`}
              style={{ '--heat': Math.min(1, value / max) } as CSSProperties}
              title={`${row.project} - ${days[index]} - ${formatCost(value)}`}
            >
              {value > 0 ? formatCostBig(value) : ''}
            </span>
          ))}
          <strong>{formatCostBig(row.total)}</strong>
        </div>
      ))}
    </div>
  );
}

export function App() {
  const [payload, setPayload] = useState<DashboardPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedRange, setSelectedRange] = useState<RangeSelection>(readRangeFromURL());
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [modelsInitialized, setModelsInitialized] = useState(false);
  const [hourlyTz, setHourlyTz] = useState<TzMode>('local');
  const [sessionSort, setSessionSort] = useState<SortState<SessionSortCol>>(initialSessionSort);
  const [modelSort, setModelSort] = useState<SortState<ModelSortCol>>(initialModelSort);
  const [projectSort, setProjectSort] = useState<SortState<ProjectSortCol>>(initialProjectSort);
  const [branchSort, setBranchSort] = useState<SortState<BranchSortCol>>(initialBranchSort);
  const [rescanMessage, setRescanMessage] = useState<string>('Rescan');
  const [rescanning, setRescanning] = useState(false);
  const autoRefreshTimer = useRef<number | null>(null);
  const rescanResetTimer = useRef<number | null>(null);

  const orderedModels = useMemo(() => {
    const next = [...(payload?.all_models ?? [])];
    next.sort((a, b) => {
      const byPriority = modelPriority(a) - modelPriority(b);
      return byPriority !== 0 ? byPriority : a.localeCompare(b);
    });
    return next;
  }, [payload?.all_models]);

  const updateURL = useCallback(() => {
    if (!payload || !modelsInitialized) return;
    const params = new URLSearchParams();
    if (selectedRange !== '30d') params.set('range', selectedRange);
    if (!isDefaultModelSelection(payload.all_models, selectedModels)) {
      params.set('models', [...selectedModels].join(','));
    }
    const query = params.toString();
    window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
  }, [modelsInitialized, payload, selectedModels, selectedRange]);

  useEffect(() => {
    updateURL();
  }, [updateURL]);

  const loadData = useCallback(async () => {
    try {
      const response = await fetch('/api/data');
      const data = (await response.json()) as DashboardPayload;
      if (data.error) {
        setLoadError(data.error);
        setPayload(null);
        setLoading(false);
        return;
      }
      setPayload(data);
      setLoadError(null);
      setLoading(false);
    } catch (error) {
      setLoadError(String(error));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (!payload || modelsInitialized) return;
    setSelectedModels(parseModelSelectionFromURL(payload.all_models));
    setModelsInitialized(true);
  }, [modelsInitialized, payload]);

  useEffect(() => {
    if (!rangeIncludesToday(selectedRange)) {
      if (autoRefreshTimer.current) {
        window.clearInterval(autoRefreshTimer.current);
        autoRefreshTimer.current = null;
      }
      return;
    }

    if (autoRefreshTimer.current) window.clearInterval(autoRefreshTimer.current);
    autoRefreshTimer.current = window.setInterval(() => {
      void loadData();
    }, 30_000);

    return () => {
      if (autoRefreshTimer.current) {
        window.clearInterval(autoRefreshTimer.current);
        autoRefreshTimer.current = null;
      }
    };
  }, [selectedRange, loadData]);

  useEffect(() => () => {
    if (rescanResetTimer.current) {
      window.clearTimeout(rescanResetTimer.current);
    }
  }, []);

  const totals = useDashboardTotals(payload, selectedModels, selectedRange, hourlyTz);

  const modelSortRows = useMemo(() => {
    if (!totals?.byModel) return [];
    return numericSort(totals.byModel, modelSort.col, modelSort.dir);
  }, [modelSort, totals?.byModel]);

  const sessionSortRows = useMemo(() => {
    if (!totals?.filteredSessions) return [];
    return [...totals.filteredSessions].sort((a, b) => {
      const left = sessionSort.col === 'cost'
        ? costForModel(a.model, a.input, a.output, a.cache_read, a.cache_creation)
        : a[sessionSort.col];
      const right = sessionSort.col === 'cost'
        ? costForModel(b.model, b.input, b.output, b.cache_read, b.cache_creation)
        : b[sessionSort.col];

      if (left < right) return sessionSort.dir === 'desc' ? 1 : -1;
      if (left > right) return sessionSort.dir === 'desc' ? -1 : 1;
      return 0;
    });
  }, [sessionSort, totals?.filteredSessions]);

  const projectSortRows = useMemo(() => {
    if (!totals?.byProject) return [];
    return numericSort(totals.byProject, projectSort.col, projectSort.dir);
  }, [projectSort, totals?.byProject]);

  const projectBranchSortRows = useMemo(() => {
    if (!totals?.byProjectBranch) return [];
    return numericSort(totals.byProjectBranch, branchSort.col, branchSort.dir);
  }, [branchSort, totals?.byProjectBranch]);

  const sessionRangeMessage = useMemo(() => {
    if (!payload || selectedRange === 'all' || !rangeIncludesToday(selectedRange)) return '';
    return 'Auto-refresh in 30s';
  }, [payload, selectedRange]);

  const operatorSignal = useMemo(() => {
    if (!totals || totals.sessions.count === 0) return 'No sessions match the current scope.';
    const topModel = totals.byModel[0];
    if (topModel && totals.sessions.cost > 0 && topModel.cost / totals.sessions.cost > 0.7) {
      return `${topModel.model} dominates estimated spend.`;
    }
    if (totals.sessions.surgeDays > 0) {
      return `${totals.sessions.surgeDays} spend surge day${totals.sessions.surgeDays === 1 ? '' : 's'} in scope.`;
    }
    if (totals.sessions.cacheShare > 0.5) {
      return 'Prompt cache is carrying most reusable context.';
    }
    return 'Spend is distributed across the selected models.';
  }, [totals]);

  const dailyChartData = useMemo(() => ({
    labels: totals?.daily.map((item) => compactDate(item.day)) ?? [],
    datasets: [
      { label: 'Input', data: totals?.daily.map((item) => item.input) ?? [], backgroundColor: TOKEN_COLORS.input, stack: 'tokens' },
      { label: 'Output', data: totals?.daily.map((item) => item.output) ?? [], backgroundColor: TOKEN_COLORS.output, stack: 'tokens' },
      { label: 'Cache Read', data: totals?.daily.map((item) => item.cache_read) ?? [], backgroundColor: TOKEN_COLORS.cache_read, stack: 'cache' },
      { label: 'Cache Write', data: totals?.daily.map((item) => item.cache_creation) ?? [], backgroundColor: TOKEN_COLORS.cache_creation, stack: 'cache' },
    ],
  }), [totals?.daily]);

  const dailyOptions = useMemo<ChartOptions<'bar'>>(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { color: '#91a0b8', boxWidth: 10, font: { size: 11 } },
        },
        tooltip: {
          callbacks: {
            label: (item) => ` ${item.dataset.label}: ${formatTokens(Number(item.raw ?? 0))}`,
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          ticks: { color: '#8794aa', maxTicksLimit: RANGE_TICKS[selectedRange] },
          grid: { color: 'rgba(119, 135, 165, 0.12)' },
        },
        y: {
          stacked: true,
          beginAtZero: true,
          ticks: { color: '#8794aa', callback: (value) => formatTokens(Number(value)) },
          grid: { color: 'rgba(119, 135, 165, 0.12)' },
        },
      },
    }),
    [selectedRange],
  );

  const projectChartData = useMemo(() => {
    const topProjects = projectSortRows.slice(0, 8);
    return {
      labels: topProjects.map((project) => project.project.length > 24 ? `...${project.project.slice(-21)}` : project.project),
      datasets: [
        { label: 'Input', data: topProjects.map((project) => project.input), backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: topProjects.map((project) => project.output), backgroundColor: TOKEN_COLORS.output },
      ],
    };
  }, [projectSortRows]);

  const projectOptions = useMemo<ChartOptions<'bar'>>(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: 'y',
      plugins: {
        legend: { labels: { color: '#91a0b8', boxWidth: 10, font: { size: 11 } } },
      },
      scales: {
        x: { ticks: { color: '#8794aa', callback: (value) => formatTokens(Number(value)) }, grid: { color: 'rgba(119, 135, 165, 0.12)' } },
        y: { ticks: { color: '#aab6c9', font: { size: 11 } }, grid: { color: 'rgba(119, 135, 165, 0.08)' } },
      },
    }),
    [],
  );

  const setSort = <TCol extends string>(
    setFn: React.Dispatch<React.SetStateAction<SortState<TCol>>>,
    col: TCol,
  ) => {
    setFn((current) => (current.col === col ? { col, dir: current.dir === 'desc' ? 'asc' : 'desc' } : { col, dir: 'desc' }));
  };

  const handleModelToggle = useCallback((model: string) => {
    setSelectedModels((current) => {
      const next = new Set(current);
      if (next.has(model)) {
        next.delete(model);
      } else {
        next.add(model);
      }
      return next;
    });
  }, []);

  const handleSelectAllModels = useCallback(() => {
    setSelectedModels(new Set(orderedModels));
  }, [orderedModels]);

  const handleClearAllModels = useCallback(() => {
    setSelectedModels(new Set());
  }, []);

  const handleRescan = useCallback(async () => {
    setRescanning(true);
    setRescanMessage('Scanning...');
    if (rescanResetTimer.current) {
      window.clearTimeout(rescanResetTimer.current);
    }

    try {
      const response = await fetch('/api/rescan', { method: 'POST' });
      const nextPayload = (await response.json()) as { new: number; updated: number; skipped: number };
      await loadData();
      setRescanMessage(`Rescan: ${nextPayload.new} new, ${nextPayload.updated} updated, ${nextPayload.skipped} skipped`);
    } catch {
      setRescanMessage('Rescan failed');
    } finally {
      rescanResetTimer.current = window.setTimeout(() => setRescanMessage('Rescan'), 3500);
      setRescanning(false);
    }
  }, [loadData]);

  const exportSessions = useCallback(() => {
    if (!totals) return;
    const headers = ['Session', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
    const rows = sessionSortRows.map((record) => {
      const cost = costForModel(record.model, record.input, record.output, record.cache_read, record.cache_creation);
      return [
        record.session_id,
        record.project,
        record.last,
        record.duration_min,
        record.model,
        record.turns,
        record.input,
        record.output,
        record.cache_read,
        record.cache_creation,
        cost.toFixed(4),
      ];
    });
    downloadCSV('sessions', headers, rows);
  }, [sessionSortRows, totals]);

  const exportProjects = useCallback(() => {
    if (!totals) return;
    const headers = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
    const rows = projectSortRows.map((record) => [
      record.project,
      record.sessions,
      record.turns,
      record.input,
      record.output,
      record.cache_read,
      record.cache_creation,
      record.cost.toFixed(4),
    ]);
    downloadCSV('projects', headers, rows);
  }, [projectSortRows, totals]);

  const exportProjectBranches = useCallback(() => {
    if (!totals) return;
    const headers = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
    const rows = projectBranchSortRows.map((record) => [
      record.project,
      record.branch,
      record.sessions,
      record.turns,
      record.input,
      record.output,
      record.cache_read,
      record.cache_creation,
      record.cost.toFixed(4),
    ]);
    downloadCSV('projects_by_branch', headers, rows);
  }, [projectBranchSortRows, totals]);

  if (loadError) {
    return (
      <main className="app-shell state-shell">
        <section className="panel error-panel">
          <h1>Claude Code Analytics</h1>
          <p>{loadError}</p>
        </section>
      </main>
    );
  }

  if (loading || !payload || !totals) {
    return (
      <main className="app-shell state-shell">
        <section className="panel loading-panel">Loading usage data...</section>
      </main>
    );
  }

  const selectedModelCount = selectedModels.size;
  const totalModelCount = orderedModels.length;
  const rangeLabel = RANGE_LABELS[selectedRange];
  const topProject = totals.byProject[0];
  const topModel = totals.byModel[0];

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">CC</span>
          <div>
            <h1>Claude Code Analytics</h1>
            <p>Local JSONL usage, cost posture, and session pressure</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className="live-pill"><StatusDot />{sessionRangeMessage || 'Static range'}</span>
          <button type="button" className="icon-button" onClick={handleRescan} disabled={rescanning}>
            {rescanMessage}
          </button>
        </div>
      </header>

      <div className="workbench">
        <aside className="control-spine panel">
          <div className="spine-section">
            <span className="section-label">Workspace</span>
            <strong>Local Claude transcripts</strong>
            <small>Updated {payload.generated_at}</small>
          </div>

          <div className="spine-section">
            <span className="section-label">Range</span>
            <div className="range-stack" role="radiogroup" aria-label="Date range">
              {VALID_RANGES.map((range) => (
                <button
                  key={range}
                  type="button"
                  className={`range-choice ${selectedRange === range ? 'active' : ''}`}
                  onClick={() => setSelectedRange(range)}
                >
                  {RANGE_LABELS[range]}
                </button>
              ))}
            </div>
          </div>

          <div className="spine-section">
            <div className="section-title-row">
              <span className="section-label">Models</span>
              <span className="count-label">{selectedModelCount}/{totalModelCount}</span>
            </div>
            <div className="model-stack">
              {orderedModels.map((model) => {
                const active = selectedModels.has(model);
                return (
                  <button
                    type="button"
                    key={model}
                    className={`model-choice ${active ? 'active' : ''}`}
                    onClick={() => handleModelToggle(model)}
                    aria-pressed={active}
                  >
                    <span>{model}</span>
                  </button>
                );
              })}
            </div>
            <div className="inline-actions">
              <button type="button" onClick={handleSelectAllModels}>All</button>
              <button type="button" onClick={handleClearAllModels}>None</button>
            </div>
          </div>

          <div className="spine-section">
            <span className="section-label">Hourly Window</span>
            <div className="segmented-control" role="radiogroup" aria-label="Hourly timezone">
              <button type="button" className={hourlyTz === 'local' ? 'active' : ''} onClick={() => setHourlyTz('local')}>Local</button>
              <button type="button" className={hourlyTz === 'utc' ? 'active' : ''} onClick={() => setHourlyTz('utc')}>UTC</button>
            </div>
            <small>{tzDisplayName(hourlyTz)}</small>
          </div>

          <div className="spine-section health-block">
            <span className="section-label">Integrity</span>
            <ShortMetric label="Latest activity" value={totals.sessions.latestActivity || 'n/a'} />
            <ShortMetric label="Cache served share" value={formatPercent(totals.sessions.cacheShare)} tone={totals.sessions.cacheShare > 0.3 ? 'good' : undefined} />
            <ShortMetric label="Surge days" value={String(totals.sessions.surgeDays)} tone={totals.sessions.surgeDays ? 'warn' : undefined} />
          </div>
        </aside>

        <section className="operations">
          <section className="posture panel">
            <div className="posture-main">
              <span className="section-label">Cost Posture - {rangeLabel}</span>
              <strong>{formatCostBig(totals.sessions.cost)}</strong>
              <p>{operatorSignal}</p>
            </div>
            <div className="posture-metrics">
              <ShortMetric label="Daily average" value={formatCostBig(totals.sessions.dailyAverageCost)} />
              <ShortMetric label="Per 1M tokens" value={formatCostBig(totals.sessions.costPerMillionTokens)} />
              <ShortMetric label="Output/input" value={`${totals.sessions.outputInputRatio.toFixed(2)}x`} />
              <ShortMetric label="Token volume" value={formatTokens(totals.sessions.tokenVolume)} />
            </div>
          </section>

          <section className="decision-strip">
            <ShortMetric label="Sessions" value={totals.sessions.count.toLocaleString()} note={rangeLabel} />
            <ShortMetric label="Turns" value={formatTokens(totals.sessions.turns)} note="assistant turns" />
            <ShortMetric label="Cache read" value={formatTokens(totals.sessions.cacheRead)} note="prompt cache" tone="good" />
            <ShortMetric label="Top project" value={topProject ? topProject.project : 'n/a'} note={topProject ? formatCostBig(topProject.cost) : undefined} />
            <ShortMetric label="Top model" value={topModel ? topModel.model : 'n/a'} note={topModel ? formatCostBig(topModel.cost) : undefined} />
          </section>

          <section className="analysis-grid">
            <article className="panel daily-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-label">Workload Pressure</span>
                  <h2>Daily token movement</h2>
                </div>
                <span className="panel-note">{totals.daily.length} day{totals.daily.length === 1 ? '' : 's'} in scope</span>
              </div>
              <div className="chart-canvas tall">
                {totals.daily.length ? <Bar options={dailyOptions} data={dailyChartData} /> : <div className="empty-state">No daily usage for this scope.</div>}
              </div>
            </article>

            <article className="panel model-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-label">Model Spend Mix</span>
                  <h2>Ranked by estimated cost</h2>
                </div>
              </div>
              <RankedModelMix rows={modelSortRows} totalCost={totals.sessions.cost} />
            </article>

            <article className="panel hourly-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-label">Burst Windows</span>
                  <h2>Hourly pressure matrix</h2>
                </div>
                <span className="panel-note">{totals.dayCount} day average</span>
              </div>
              <HourlyPressure buckets={totals.hourlyByHour} tz={hourlyTz} />
            </article>

            <article className="panel project-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-label">Project Heatmap</span>
                  <h2>Spend by turn date</h2>
                </div>
                <button type="button" className="text-button" onClick={exportProjects}>Export CSV</button>
              </div>
              <ProjectHeatmap days={totals.heatmapDays} rows={totals.heatmapRows} max={totals.heatmapMax} />
            </article>

            <article className="panel project-chart-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-label">Project Compare</span>
                  <h2>Input and output concentration</h2>
                </div>
              </div>
              <div className="chart-canvas">
                {projectSortRows.length ? <Bar options={projectOptions} data={projectChartData} /> : <div className="empty-state">No project rows available.</div>}
              </div>
            </article>
          </section>

          <section className="table-shell panel">
            <div className="table-title-row">
              <div>
                <span className="section-label">Recent Session Ledger</span>
                <h2>Last active sessions</h2>
              </div>
              <button type="button" className="text-button" onClick={exportSessions}>Export CSV</button>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Project</th>
                    <th>Branch</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'last')}>Last {sessionSort.col === 'last' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'duration_min')}>Duration {sessionSort.col === 'duration_min' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    <th>Model</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'turns')}>Turns {sessionSort.col === 'turns' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'input')}>Input {sessionSort.col === 'input' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'output')}>Output {sessionSort.col === 'output' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    <th className="sortable" onClick={() => setSort(setSessionSort, 'cost')}>Cost {sessionSort.col === 'cost' ? (sessionSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                  </tr>
                </thead>
                <tbody>
                  {sessionSortRows.slice(0, 30).map((session) => {
                    const cost = costForModel(session.model, session.input, session.output, session.cache_read, session.cache_creation);
                    return (
                      <tr key={session.session_id}>
                        <td className="monospace" title={session.session_id}>{session.session_label}</td>
                        <td>{session.project}</td>
                        <td className="muted">{session.branch || '-'}</td>
                        <td className="muted">{session.last}</td>
                        <td>{formatDuration(session.duration_min)}</td>
                        <td><span className="model-tag">{session.model}</span></td>
                        <td>{formatTokens(session.turns)}</td>
                        <td>{formatTokens(session.input)}</td>
                        <td>{formatTokens(session.output)}</td>
                        <td>{isBillable(session.model) ? formatCost(cost) : 'n/a'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="table-grid">
            <article className="table-shell panel">
              <div className="table-title-row">
                <div>
                  <span className="section-label">Model Contract</span>
                  <h2>Cost by model</h2>
                </div>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'turns')}>Turns {modelSort.col === 'turns' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'input')}>Input {modelSort.col === 'input' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'output')}>Output {modelSort.col === 'output' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'cache_read')}>Cache Read {modelSort.col === 'cache_read' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'cache_creation')}>Cache Write {modelSort.col === 'cache_creation' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setModelSort, 'cost')}>Cost {modelSort.col === 'cost' ? (modelSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modelSortRows.slice(0, 20).map((model) => (
                      <tr key={model.model}>
                        <td><span className="model-tag">{model.model}</span></td>
                        <td>{formatTokens(model.turns)}</td>
                        <td>{formatTokens(model.input)}</td>
                        <td>{formatTokens(model.output)}</td>
                        <td>{formatTokens(model.cache_read)}</td>
                        <td>{formatTokens(model.cache_creation)}</td>
                        <td>{isBillable(model.model) ? formatCost(model.cost) : 'n/a'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </article>

            <article className="table-shell panel">
              <div className="table-title-row">
                <div>
                  <span className="section-label">Branch Drilldown</span>
                  <h2>Project + branch usage</h2>
                </div>
                <button type="button" className="text-button" onClick={exportProjectBranches}>Export CSV</button>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Project</th>
                      <th>Branch</th>
                      <th className="sortable" onClick={() => setSort(setBranchSort, 'sessions')}>Sessions {branchSort.col === 'sessions' ? (branchSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setBranchSort, 'turns')}>Turns {branchSort.col === 'turns' ? (branchSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setBranchSort, 'input')}>Input {branchSort.col === 'input' ? (branchSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setBranchSort, 'output')}>Output {branchSort.col === 'output' ? (branchSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                      <th className="sortable" onClick={() => setSort(setBranchSort, 'cost')}>Cost {branchSort.col === 'cost' ? (branchSort.dir === 'desc' ? 'v' : '^') : ''}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {projectBranchSortRows.slice(0, 24).map((projectBranch) => (
                      <tr key={`${projectBranch.project}|${projectBranch.branch}`}>
                        <td>{projectBranch.project}</td>
                        <td className="muted">{projectBranch.branch || '-'}</td>
                        <td>{projectBranch.sessions}</td>
                        <td>{formatTokens(projectBranch.turns)}</td>
                        <td>{formatTokens(projectBranch.input)}</td>
                        <td>{formatTokens(projectBranch.output)}</td>
                        <td>{formatCost(projectBranch.cost)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </article>
          </section>
        </section>
      </div>
    </main>
  );
}
