import { ChevronDown, Send, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { API_URL, streamMessages } from "@/api/client";
import { useConversation, useCreateConversation } from "@/api/hooks";
import { PageHeader } from "@/components/Layout";
import { Button, Card, CardContent, Input } from "@/components/ui/primitives";

interface ToolCall {
  name: string;
  arguments: unknown;
  error?: string | null;
}

interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: ToolCall[];
  streaming?: boolean;
}

const SUGGESTIONS = [
  "Which VMs are idle?",
  "How much CPU can I reclaim on cluster DC0_C0?",
  "When will datastore LocalDS_0 fill up?",
  "What anomalies happened this week?",
];

function ToolCalls({ tools }: { tools: ToolCall[] }) {
  const [open, setOpen] = useState(false);
  if (tools.length === 0) return null;
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
      >
        <ChevronDown
          className={open ? "h-3 w-3 rotate-180 transition-transform" : "h-3 w-3 transition-transform"}
          aria-hidden
        />
        <Wrench className="h-3 w-3" aria-hidden />
        {tools.length} tool call{tools.length === 1 ? "" : "s"} used
      </button>
      {open ? (
        <ul className="mt-1 space-y-1">
          {tools.map((tool, index) => (
            <li
              key={`${tool.name}-${index}`}
              className="rounded border border-border/60 bg-background/60 p-2 font-mono text-[11px]"
            >
              <span className={tool.error ? "text-destructive" : "text-primary"}>{tool.name}</span>
              <span className="text-muted-foreground">
                ({JSON.stringify(tool.arguments ?? {})})
              </span>
              {tool.error ? <p className="mt-1 text-destructive">{tool.error}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function AssistantPage() {
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const createConversation = useCreateConversation();
  const conversation = useConversation(conversationId);

  // Replay persisted history when an existing conversation loads.
  useEffect(() => {
    if (!conversation.data || turns.length > 0) return;
    const replayed: Turn[] = [];
    let pendingTools: ToolCall[] = [];
    for (const message of conversation.data.messages) {
      if (message.role === "tool") {
        pendingTools.push({ name: message.tool_name ?? "tool", arguments: message.tool_args });
      } else if (message.role === "assistant") {
        replayed.push({ role: "assistant", content: message.content, tools: pendingTools });
        pendingTools = [];
      } else {
        replayed.push({ role: "user", content: message.content });
      }
    }
    if (replayed.length) setTurns(replayed);
  }, [conversation.data, turns.length]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy) return;

    setError(null);
    setBusy(true);
    setInput("");

    let id = conversationId;
    try {
      if (!id) {
        const created = await createConversation.mutateAsync(question.slice(0, 60));
        id = created.id;
        setConversationId(id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start a conversation");
      setBusy(false);
      return;
    }

    setTurns((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "", tools: [], streaming: true },
    ]);

    const update = (mutate: (turn: Turn) => Turn) =>
      setTurns((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant") next[next.length - 1] = mutate(last);
        return next;
      });

    await streamMessages(
      `/assistant/conversations/${id}/messages`,
      { content: question },
      {
        onTool: (tool) => update((turn) => ({ ...turn, tools: [...(turn.tools ?? []), tool] })),
        onToken: (chunk) => update((turn) => ({ ...turn, content: turn.content + chunk })),
        onError: (message) => {
          setError(message);
          update((turn) => ({ ...turn, streaming: false }));
        },
        onDone: () => update((turn) => ({ ...turn, streaming: false })),
      },
    );

    setBusy(false);
  }

  return (
    <>
      <PageHeader
        title="Assistant"
        description="Read-only. Answers come from the platform's own data, via tool calls to a local model."
      />

      <div className="flex h-[calc(100vh-4rem)] flex-col">
        <div className="flex-1 space-y-4 overflow-y-auto p-6">
          {turns.length === 0 ? (
            <div className="mx-auto max-w-2xl space-y-3 pt-8 text-center">
              <h2 className="text-base font-medium">Ask about your estate</h2>
              <p className="text-sm text-muted-foreground">
                The assistant can only read. It cannot power on, resize or migrate anything.
              </p>
              <div className="flex flex-wrap justify-center gap-2 pt-2">
                {SUGGESTIONS.map((suggestion) => (
                  <Button
                    key={suggestion}
                    variant="outline"
                    size="sm"
                    onClick={() => void send(suggestion)}
                  >
                    {suggestion}
                  </Button>
                ))}
              </div>
            </div>
          ) : null}

          {turns.map((turn, index) => (
            <div
              key={index}
              className={turn.role === "user" ? "flex justify-end" : "flex justify-start"}
            >
              <Card
                className={
                  turn.role === "user"
                    ? "max-w-2xl bg-primary/10 border-primary/20"
                    : "max-w-3xl"
                }
              >
                <CardContent className="p-3">
                  <p className="whitespace-pre-wrap text-sm leading-relaxed">
                    {turn.content}
                    {turn.streaming ? (
                      <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-primary align-middle" />
                    ) : null}
                  </p>
                  {turn.role === "assistant" ? <ToolCalls tools={turn.tools ?? []} /> : null}
                </CardContent>
              </Card>
            </div>
          ))}

          {error ? (
            <div
              className="mx-auto max-w-2xl rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
              role="alert"
            >
              <p className="font-medium">Assistant unavailable</p>
              <p className="mt-1 text-xs">{error}</p>
              <p className="mt-1 text-[11px] opacity-80">
                The LLM runs in an optional compose profile:{" "}
                <code>docker compose --profile llm up -d llm</code>. API: {API_URL}
              </p>
            </div>
          ) : null}

          <div ref={bottomRef} />
        </div>

        <form
          className="flex items-center gap-2 border-t border-border p-4"
          onSubmit={(event) => {
            event.preventDefault();
            void send(input);
          }}
        >
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about VMs, capacity, anomalies or recommendations…"
            disabled={busy}
          />
          <Button type="submit" disabled={busy || !input.trim()}>
            <Send className="h-4 w-4" aria-hidden />
            {busy ? "Thinking…" : "Send"}
          </Button>
        </form>
      </div>
    </>
  );
}
