'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  ArrowUpRight,
  Bot,
  Braces,
  Check,
  CircleDot,
  Clock3,
  Code2,
  Copy,
  FileJson,
  Globe2,
  History,
  Layers3,
  LoaderCircle,
  MousePointer2,
  Play,
  Plus,
  Radar,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  SquareArrowOutUpRight,
  TerminalSquare,
  TestTube2,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

type Mode = 'automate' | 'scrape' | 'verify' | 'monitor';
type OutputFormat = 'summary' | 'json' | 'markdown';
type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

type RunEvent = { at: string; kind: string; message: string; url?: string };
type AgentRun = {
  run_id: string;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  target_url: string;
  instructions: string;
  mode: string;
  output_format: string;
  safe_mode: boolean;
  max_steps: number;
  progress: number;
  live_view_url?: string;
  session_id?: string;
  result?: unknown;
  error?: string;
  events: RunEvent[];
};

const API_URL =
  process.env.NEXT_PUBLIC_AGENT_API_URL ??
  'http://localhost:8000';

const modes: Array<{ id: Mode; label: string; icon: typeof MousePointer2 }> = [
  { id: 'automate', label: 'Automate', icon: MousePointer2 },
  { id: 'scrape', label: 'Scrape', icon: Braces },
  { id: 'verify', label: 'Verify', icon: TestTube2 },
  { id: 'monitor', label: 'Monitor', icon: Radar },
];

const examples: Record<Mode, string> = {
  automate:
    'Open the pricing page, switch to annual billing, and compare the Pro and Team plans. Do not submit or purchase anything.',
  scrape:
    'Extract every plan, monthly price, included seats, and usage limit. Return clean JSON and flag plans without a listed price.',
  verify:
    'Verify that the primary navigation, pricing toggle, and contact-sales button are visible and usable. Report pass, fail, or blocked.',
  monitor:
    'Capture the current plan names and prices as a change-detection snapshot, including the page timestamp and source URL.',
};

const outputFormats: OutputFormat[] = ['summary', 'json', 'markdown'];

function formatResult(value: unknown) {
  if (typeof value === 'string') return value;
  return JSON.stringify(value ?? {}, null, 2);
}

export default function Home() {
  const [mode, setMode] = useState<Mode>('scrape');
  const [targetUrl, setTargetUrl] = useState('https://example.com');
  const [instructions, setInstructions] = useState(examples.scrape);
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('json');
  const [safeMode, setSafeMode] = useState(true);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [runtime, setRuntime] = useState<'checking' | 'ready' | 'degraded'>(
    'checking',
  );

  const isActive = run?.status === 'queued' || run?.status === 'running';
  const visibleEvents = useMemo(() => run?.events.slice(-6).reverse() ?? [], [run]);

  const pollRun = useCallback(async (runId: string) => {
    const response = await fetch(`${API_URL}/api/v1/agent/runs/${runId}`, {
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('Could not refresh this run');
    const nextRun = (await response.json()) as AgentRun;
    setRun(nextRun);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    void fetch(`${API_URL}/health`, {
      cache: 'no-store',
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error('Backend unavailable');
        const health = (await response.json()) as { status?: string };
        setRuntime(health.status === 'ok' ? 'ready' : 'degraded');
      })
      .catch(() => setRuntime('degraded'))
      .finally(() => window.clearTimeout(timeout));
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (!run || !isActive) return;
    const timer = window.setInterval(() => {
      void pollRun(run.run_id).catch((pollError: Error) =>
        setError(pollError.message),
      );
    }, 2500);
    return () => window.clearInterval(timer);
  }, [isActive, pollRun, run]);

  function chooseMode(nextMode: Mode) {
    setMode(nextMode);
    setInstructions(examples[nextMode]);
    setOutputFormat(nextMode === 'scrape' || nextMode === 'monitor' ? 'json' : 'summary');
  }

  function cycleOutput() {
    const currentIndex = outputFormats.indexOf(outputFormat);
    setOutputFormat(outputFormats[(currentIndex + 1) % outputFormats.length]);
  }

  async function startRun() {
    setError('');
    setSubmitting(true);
    try {
      const response = await fetch(`${API_URL}/api/v1/agent/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_url: targetUrl,
          instructions,
          mode,
          output_format: outputFormat,
          safe_mode: safeMode,
          max_steps: 20,
        }),
      });
      const payload = (await response.json()) as AgentRun & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail ?? 'The run could not be started');
      }
      setRun(payload as AgentRun);
    } catch (submitError) {
      const message =
        submitError instanceof TypeError
          ? 'The agent backend is offline or not allowing this site origin.'
          : submitError instanceof Error
            ? submitError.message
            : 'The run could not be started';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }

  async function copyResult() {
    await navigator.clipboard.writeText(formatResult(run?.result));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="flex h-16 items-center border-b border-white/[0.08] px-4 lg:px-6">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex size-8 items-center justify-center rounded-lg border border-lime-300/25 bg-lime-300 text-[#10130c] shadow-[0_0_28px_rgba(190,242,100,0.14)]">
            <Bot className="size-[17px]" strokeWidth={2.4} />
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-[15px] font-semibold tracking-[-0.02em]">Waypoint</span>
            <span className="hidden text-[11px] font-medium uppercase tracking-[0.16em] text-zinc-600 sm:inline">Browser agent</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.025] px-3 py-1.5 text-xs text-zinc-400">
            <span className={`size-1.5 rounded-full ${runtime === 'degraded' || run?.status === 'failed' ? 'bg-red-400' : runtime === 'checking' ? 'bg-amber-300' : 'bg-lime-300 shadow-[0_0_8px_rgba(190,242,100,0.8)]'}`} />
            {isActive
              ? 'Agent running'
              : runtime === 'checking'
                ? 'Checking runtime'
                : runtime === 'degraded'
                  ? 'Runtime unavailable'
                  : 'Runtime ready'}
          </div>
        </div>
      </header>

      <div className="grid min-h-[calc(100vh-4rem)] lg:grid-cols-[72px_minmax(390px,0.82fr)_minmax(430px,1.18fr)]">
        <aside className="hidden flex-col items-center border-r border-white/[0.08] py-4 lg:flex">
          <nav className="flex flex-col gap-2" aria-label="Primary navigation">
            <Button size="icon" className="bg-lime-300 text-[#10130c] hover:bg-lime-200" aria-label="New run" onClick={() => setRun(null)}><Plus /></Button>
            <Button variant="ghost" size="icon" className="text-zinc-500 hover:bg-white/[0.05] hover:text-zinc-200" aria-label="History"><History /></Button>
            <Button variant="ghost" size="icon" className="text-zinc-500 hover:bg-white/[0.05] hover:text-zinc-200" aria-label="Templates"><Layers3 /></Button>
            <Button variant="ghost" size="icon" className="text-zinc-500 hover:bg-white/[0.05] hover:text-zinc-200" aria-label="Developer tools"><Code2 /></Button>
          </nav>
          <div className="mt-auto flex size-8 items-center justify-center rounded-full border border-white/[0.08] bg-[#272a22] text-lime-200"><Globe2 className="size-3.5" /></div>
        </aside>

        <section className="border-r border-white/[0.08] p-4 sm:p-6 lg:p-8">
          <div className="mx-auto max-w-[580px]">
            <Badge variant="outline" className="mb-4 border-lime-300/20 bg-lime-300/[0.06] text-lime-200"><Sparkles /> New automation</Badge>
            <h1 className="max-w-md text-3xl font-semibold leading-[1.08] tracking-[-0.045em] text-zinc-100 sm:text-[38px]">What should the browser get done?</h1>
            <p className="mt-3 max-w-lg text-sm leading-6 text-zinc-500">Give Waypoint a public website and an outcome. It plans, acts, verifies, and returns a trace you can inspect.</p>

            <fieldset className="mb-5 mt-8 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <legend className="sr-only">Automation mode</legend>
              {modes.map((item) => {
                const Icon = item.icon;
                const selected = item.id === mode;
                return (
                  <button key={item.id} onClick={() => chooseMode(item.id)} aria-pressed={selected} className={`flex h-[74px] flex-col justify-between rounded-xl border p-3 text-left transition-colors ${selected ? 'border-lime-300/45 bg-lime-300/[0.07] text-lime-200' : 'border-white/[0.08] bg-white/[0.025] text-zinc-500 hover:border-white/[0.14] hover:text-zinc-300'}`}>
                    <Icon className="size-4" /><span className="text-xs font-medium">{item.label}</span>
                  </button>
                );
              })}
            </fieldset>

            <div className="space-y-4 rounded-2xl border border-white/[0.09] bg-[#171914] p-4 shadow-[0_24px_60px_rgba(0,0,0,0.2)] sm:p-5">
              <label className="block" htmlFor="target-url">
                <span className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500"><Globe2 className="size-3.5" /> Target website</span>
                <Input id="target-url" value={targetUrl} onChange={(event) => setTargetUrl(event.target.value)} className="h-11 border-white/[0.09] bg-black/20 px-3 text-sm text-zinc-200 focus-visible:border-lime-300/45 focus-visible:ring-lime-300/10" aria-label="Target website" placeholder="https://example.com" inputMode="url" />
              </label>

              <label className="block" htmlFor="agent-instructions">
                <span className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500"><TerminalSquare className="size-3.5" /> Instructions</span>
                <Textarea id="agent-instructions" value={instructions} onChange={(event) => setInstructions(event.target.value)} className="min-h-[126px] resize-none border-white/[0.09] bg-black/20 p-3.5 text-sm leading-6 text-zinc-200 focus-visible:border-lime-300/45 focus-visible:ring-lime-300/10" aria-label="Automation instructions" />
              </label>

              {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-400/20 bg-red-400/[0.06] p-3 text-xs leading-5 text-red-200"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" />{error}</div>}

              <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.07] pt-4">
                <button onClick={cycleOutput} className="flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs capitalize text-zinc-500 hover:bg-white/[0.04] hover:text-zinc-300"><FileJson className="size-4" /> {outputFormat}</button>
                <button onClick={() => setSafeMode((value) => !value)} aria-pressed={safeMode} className={`flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs ${safeMode ? 'bg-lime-300/[0.06] text-lime-200' : 'text-zinc-500 hover:bg-white/[0.04] hover:text-zinc-300'}`}><ShieldCheck className="size-4" /> Safe mode</button>
                <Button onClick={startRun} disabled={submitting || isActive || !targetUrl || instructions.length < 8} className="ml-auto h-10 rounded-lg bg-lime-300 px-4 text-[#10130c] shadow-[0_0_28px_rgba(190,242,100,0.12)] hover:bg-lime-200">
                  {submitting || isActive ? <LoaderCircle className="animate-spin" /> : <Play className="fill-current" />} {isActive ? 'Running' : 'Run agent'}
                </Button>
              </div>
            </div>
            <p className="mt-4 flex items-center gap-2 text-xs text-zinc-600"><ShieldCheck className="size-3.5" /> Anonymous runs stay on one public site. Payments, logins, messaging, and destructive actions are blocked.</p>
          </div>
        </section>

        <section className="min-w-0 bg-[#0d0f0b] p-4 sm:p-6 lg:p-8">
          <div className="mx-auto max-w-[760px]">
            <div className="mb-5 flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2"><h2 className="text-sm font-semibold text-zinc-200">{run ? 'Live run' : 'Agent workspace'}</h2>{run && <Badge className={run.status === 'completed' ? 'bg-lime-300/10 text-lime-200' : run.status === 'failed' ? 'bg-red-400/10 text-red-200' : 'bg-amber-400/10 text-amber-200'}>{run.status}</Badge>}</div>
                <p className="mt-1 text-xs text-zinc-600">{run ? `Run ${run.run_id} · ${run.progress}% complete` : 'Execution trace and verified output appear here'}</p>
              </div>
              {run && <Button variant="ghost" size="icon" onClick={() => void pollRun(run.run_id)} className="text-zinc-600 hover:bg-white/[0.05] hover:text-zinc-300" aria-label="Refresh run"><RefreshCw /></Button>}
            </div>

            <div className="overflow-hidden rounded-2xl border border-white/[0.09] bg-[#151713]">
              <div className="flex h-11 items-center gap-1.5 border-b border-white/[0.08] px-4">
                <span className="size-2 rounded-full bg-[#ff6b63]" /><span className="size-2 rounded-full bg-[#f4be4f]" /><span className="size-2 rounded-full bg-[#65c466]" />
                <div className="mx-auto flex max-w-[420px] flex-1 items-center justify-center gap-2 truncate rounded-md bg-white/[0.035] px-3 py-1 text-[10px] text-zinc-600"><Globe2 className="size-3 shrink-0" /> {run?.target_url ?? targetUrl}</div>
              </div>

              {run?.live_view_url && isActive ? (
                <div className="grid min-h-[300px] place-items-center bg-[radial-gradient(circle_at_50%_35%,rgba(190,242,100,0.09),transparent_45%)] p-8 text-center">
                  <div><div className="mx-auto flex size-12 items-center justify-center rounded-full border border-lime-300/20 bg-lime-300/[0.07] text-lime-200"><LoaderCircle className="size-5 animate-spin" /></div><h3 className="mt-4 text-base font-semibold text-zinc-200">The agent is browsing now</h3><p className="mx-auto mt-2 max-w-sm text-xs leading-5 text-zinc-500">Watch the isolated browser live while Waypoint works. The session closes automatically when the run ends.</p><a href={run.live_view_url} target="_blank" rel="noreferrer" className="mt-5 inline-flex items-center gap-2 rounded-lg border border-lime-300/20 bg-lime-300/[0.07] px-3 py-2 text-xs font-medium text-lime-200 hover:bg-lime-300/[0.12]">Open live browser <SquareArrowOutUpRight className="size-3.5" /></a></div>
                </div>
              ) : (
                <div className="grid min-h-[300px] place-items-center bg-[radial-gradient(circle_at_50%_35%,rgba(190,242,100,0.07),transparent_46%)] p-8">
                  <div className="w-full max-w-[470px] text-center"><p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-lime-200/70">Observe · act · verify</p><h3 className="mt-2 text-2xl font-semibold tracking-[-0.04em] text-zinc-100">A browser run you can actually inspect</h3><p className="mx-auto mt-3 max-w-sm text-xs leading-5 text-zinc-500">Every action becomes a concise event. Outputs include the final data, source URL, and a recorded cloud-browser session.</p><div className="mt-7 grid grid-cols-3 gap-3 text-left">{['Isolated session', 'Same-site guard', 'Verified result'].map((label, index) => <div key={label} className={`rounded-xl border p-3 ${index === 1 ? 'border-lime-300/40 bg-lime-300/[0.06]' : 'border-white/[0.08] bg-black/10'}`}><span className="text-[10px] font-medium text-zinc-400">{label}</span><div className="mt-5 h-1.5 w-full rounded-full bg-zinc-800" /><div className="mt-1.5 h-1.5 w-3/5 rounded-full bg-zinc-800" /></div>)}</div></div>
                </div>
              )}
            </div>

            <div className="mt-4 grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
              <div className="rounded-2xl border border-white/[0.09] bg-[#151713] p-4">
                <div className="mb-4 flex items-center justify-between"><span className="text-xs font-semibold text-zinc-300">Execution trace</span><span className="flex items-center gap-1.5 text-[10px] text-zinc-600"><Clock3 className="size-3" /> {run ? `${run.progress}%` : 'Ready'}</span></div>
                {visibleEvents.length ? (
                  <ol className="space-y-4">{visibleEvents.map((event, index) => <li key={`${event.at}-${index}`} className="relative flex gap-3">{index < visibleEvents.length - 1 && <span className="absolute left-[7px] top-5 h-7 w-px bg-white/[0.08]" />}<span className={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full ${event.kind === 'failed' ? 'bg-red-400/15 text-red-200' : event.kind === 'completed' ? 'bg-lime-300 text-[#10130c]' : 'border border-amber-300/50 bg-amber-300/10 text-amber-200'}`}>{event.kind === 'completed' ? <Check className="size-2.5" strokeWidth={3} /> : <CircleDot className="size-2.5" />}</span><div className="min-w-0"><p className="text-xs text-zinc-300">{event.message}</p>{event.url && <p className="mt-0.5 truncate text-[10px] text-zinc-600">{event.url}</p>}</div></li>)}</ol>
                ) : <p className="py-8 text-center text-xs leading-5 text-zinc-600">Start a run to see navigation, inspection, action, and verification events.</p>}
              </div>

              <div className="rounded-2xl border border-white/[0.09] bg-[#151713] p-4">
                <div className="mb-3 flex items-center justify-between"><span className="text-xs font-semibold text-zinc-300">Verified result</span>{run?.result != null && <button onClick={copyResult} className="flex items-center gap-1 text-[10px] text-lime-200 hover:text-lime-100">{copied ? 'Copied' : 'Copy'} <Copy className="size-3" /></button>}</div>
                {run?.status === 'completed' ? <pre className="max-h-[320px] overflow-auto whitespace-pre-wrap rounded-lg border border-white/[0.06] bg-black/25 p-3 font-mono text-[10px] leading-5 text-zinc-400"><code>{formatResult(run.result)}</code></pre> : run?.status === 'failed' ? <div className="rounded-lg border border-red-400/15 bg-red-400/[0.05] p-3 text-xs leading-5 text-red-200">{run.error ?? 'The browser run failed before it returned a result.'}</div> : <div className="rounded-lg border border-white/[0.06] bg-black/20 p-3"><div className="h-2 w-2/5 rounded-full bg-zinc-800" /><div className="mt-3 h-1.5 w-full rounded-full bg-zinc-800" /><div className="mt-2 h-1.5 w-4/5 rounded-full bg-zinc-800" /><p className="mt-6 flex items-center gap-2 text-[10px] text-zinc-600"><ArrowUpRight className="size-3" /> Output arrives after verification</p></div>}
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
