# ShivaGPT — Explain Like I'm 5

A plain-language tour of what this thing is and how it works. No jargon
unless we stop and define it.

## What is ShivaGPT, in one sentence?

It's a private ChatGPT-style chat app that runs on your own computer at
home, so nothing you type ever leaves your house.

## Why bother building it when ChatGPT exists?

Three reasons:

1. **Privacy.** Whatever you ask ShivaGPT — work documents, half-formed
   ideas, embarrassing questions — stays inside your home. It never gets
   sent to OpenAI, Anthropic, Google, or anyone else.
2. **Free to use.** No monthly subscription, no per-message cost. Once
   it's set up, you can chat for as long as you want.
3. **Offline-capable.** Internet is down? ShivaGPT still works. The AI
   model lives on your DGX, not in the cloud.

The trade-off: the AI isn't quite as smart as the very best paid ones
(GPT-4, Claude, Gemini), but it's plenty good for most everyday work
— writing, coding help, brainstorming, summarizing.

## The pieces

There are two parts. Think of it like a restaurant:

```
   Your phone or Mac (the customer)        Kailash (the kitchen)
  ┌───────────────────────┐              ┌──────────────────────┐
  │      ShivaGPT         │              │    Ollama + AI       │
  │   (the chat window)   │  ───────►    │  (the actual brain)  │
  │                       │  ◄───────    │                      │
  └───────────────────────┘              └──────────────────────┘
        Safari, Chrome,                     Sitting on the desk,
        or the Mac app                      humming with GPUs
```

- **Kailash** is your Nvidia DGX Spark — a small, very powerful computer
  that lives at home. It runs **Ollama**, which is the program that
  actually thinks (the "brain" part). You don't interact with Kailash
  directly; you just leave it switched on.
- **The chat window** is the friendly part you see and type into. It
  runs in any web browser (or as a real Mac app), and talks to Kailash
  over your home network whenever you send a message.

## How does it work when you send a message?

1. You type "Write me a haiku about my dog" and hit Enter.
2. The chat window sends those words across your home network to Kailash.
3. Kailash hands the words to the AI model (DeepSeek, Llama 3, etc.).
4. The model writes the haiku one word at a time.
5. As each word appears, it's sent back to your screen so you can watch
   the answer being typed in front of you (this is called "streaming").
6. When it's done, ShivaGPT remembers the whole conversation in your
   browser, so you can come back to it later.

Nothing is ever uploaded to the internet during any of this.

## What are "models"? Why are there several?

Think of models like different chefs. They're all trained to cook food
(answer questions), but they have different specialties and styles.

- **deepseek** — strong at reasoning and coding. Good default.
- **qwen** — multilingual, also good at coding.
- **llama3** — Meta's general-purpose model, well-rounded.

You can switch between them per conversation by clicking the model name
at the top of the chat. Different chefs for different meals.

## What are "tokens" and why does the app count them?

A token is roughly a word or a piece of a word — "elephant" is one token,
"unbelievable" is two or three. AI models think in tokens, not letters.

The little bar in the top-right shows how many tokens the current
conversation has used compared to the model's "memory window" (called the
**context window**). When the bar fills up, the model starts forgetting
the oldest parts of the conversation. If that bar turns orange or red,
start a new chat for the next topic.

## Can I attach files?

Yes. There's a paperclip icon in the message box. Drop a PDF, CSV,
screenshot (PNG/JPG), or text file in there — or just paste an image
straight from your clipboard with Cmd-V.

- **PDFs and CSVs**: ShivaGPT pulls the text out and quietly hands it to
  the AI along with your question. Good for things like "summarize this
  contract" or "what's the average of column B?"
- **Images**: ShivaGPT switches to a vision-capable model (Qwen 2.5-VL by
  default — you'll see a quick toast when it switches) and asks the
  question about the image. Good for things like "what's wrong with this
  error screenshot?" or "rewrite the text in this picture."

While a file uploads you'll see a progress bar inside the chip. After it
hits 100%, "extracting…" means the DGX is reading the file. If anything
goes wrong, the chip turns red with a clear reason — and you get a Retry
button (↻) without having to pick the file again.

## What about my chat history?

It's saved in your browser's local storage — like a private notebook
that lives on your Mac or iPhone, not on a server. If you clear your
browser data, your chats disappear. They're never uploaded anywhere.

You can also export any chat as a Markdown file, plain text, or JSON via
the download button at the top of the chat.

## What is a "system prompt"?

It's a hidden instruction at the start of a conversation that tells the
AI how to behave. Things like:

- "You are a senior engineer. Be direct and show code."
- "Reply in haikus only."
- "Pretend you are a patient tutor for a 10-year-old."

ShivaGPT comes with a library of presets you can pick from (the notebook
icon in the top bar), or you can write your own.

## What if Kailash is off?

Then the chat window will say "Cannot reach server." Turn Kailash back on
and the green dot will return within ~30 seconds. The chat window itself
keeps working — you can browse old conversations and read the history,
just not send new messages.

## Glossary

- **Ollama** — the program on Kailash that runs the AI. Like a recipe
  book and oven combined.
- **Model** — a specific trained AI. Different models have different
  strengths.
- **Token** — the smallest chunk of text the AI thinks in (~3/4 of a word).
- **Context window** — how much conversation the model can remember at
  once. Measured in tokens.
- **System prompt** — invisible instructions at the start of a chat that
  shape how the AI behaves.
- **Streaming** — words appearing one at a time as they're generated,
  instead of waiting for the whole answer at once.
- **localStorage** — a small private database inside your browser. That's
  where your chat history lives.
- **DGX** — Nvidia's brand of high-end AI computers. Your Kailash is one.
- **Tailscale** — software that makes Kailash reachable from your Mac and
  phone even when you're not at home, via a private encrypted network
  that only your devices can join.

## What's coming

The next two things on the roadmap:

1. **Make pictures.** Type "draw a watercolor of the kitchen at sunset"
   and ShivaGPT will create the image on Kailash. (Uses an open-source
   model called Flux.1-dev. Free, runs on the DGX.)
2. **Edit pictures.** Upscale a small photo to print quality, erase a
   stranger from a background, remove a watermark, swap a sky.
   (Uses an open-source tool called IOPaint.)

Both run entirely on Kailash — same privacy story, same offline behavior.

## TL;DR

You have a tiny ChatGPT in your closet. It speaks only when spoken to, it
doesn't tell anyone what you said, and it costs nothing to run. The chat
window on your Mac or phone is just a nice front door to talk to it.
