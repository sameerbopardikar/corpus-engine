## Outcome

Built a read-only, evidence-ranked expansion map for an **Agentic Engineering corpus**, prioritizing demonstrated builders, maintainers, researchers, and production operators over generic AI influencers.

**Evidence snapshot:** 2026-07-16 UTC.  
**Ranking principle:** maintained implementation or benchmark > peer-reviewed/reproducible research > production postmortem/evaluation practice > tutorials/social commentary. GitHub popularity was used only as a discovery signal.

### Evidence grades

- **A1:** Active implementation plus research, benchmarks, or documented production practice.
- **A2:** Active implementation with strong first-party technical material.
- **B1:** High-value operator/research synthesis backed by artifacts or experiments.
- **B2:** Useful education, but weaker independent implementation evidence.
- **C:** Commentary or marketing without inspectable artifacts; do not auto-promote.

---

## Ranked creator/operator expansion candidates

| Rank | Creator/operator | Primary evidence and canonical sources | Grade | Promotion rationale |
|---:|---|---|:---:|---|
| **1** | **Simon Willison** — X [@simonw](https://x.com/simonw), [GitHub](https://github.com/simonw), [site](https://simonwillison.net/) | Publishes and demonstrates disciplined coding-agent workflows, TDD, asynchronous code research, and verification. Start with [Agentic Engineering Patterns](https://simonwillison.net/2026/Feb/23/agentic-engineering-patterns/) and his [research repository](https://github.com/simonw/research). | **A1** | Best direct bridge from “vibe coding” to evidence-driven **agentic engineering**. Highly inspectable claims and unusually strong source hygiene. |
| **2** | **Hamel Husain + Shreya Shankar** — X [@HamelHusain](https://x.com/HamelHusain), [@sh_reya](https://x.com/sh_reya); GitHub [hamelsmu](https://github.com/hamelsmu), [shreyashankar](https://github.com/shreyashankar) | Production evals, trace review, failure taxonomies, human feedback, data-agent benchmarks, and DocETL. Video evidence includes Hamel’s [YouTube channel](https://www.youtube.com/channel/UC__dUuqF5w4OnbW221JxmKg) and their [evals discussion](https://www.youtube.com/watch?v=BsWxPI9UM4c). Shreya’s artifact: [DocETL](https://github.com/ucbepic/docetl). | **A1** | Essential counterweight to framework-first material. Add as a paired cluster for evaluation, product quality, trace analysis, and human-centered reliability. |
| **3** | **Harrison Chase / LangGraph maintainers** — X [@hwchase17](https://x.com/hwchase17), [GitHub](https://github.com/hwchase17) | [LangGraph](https://github.com/langchain-ai/langgraph) is an actively maintained durable agent runtime; live API snapshot showed ~37k stars. Harrison’s first-principles runtime material covers persistence, memory, human intervention, and observability. Official [LangChain YouTube](https://www.youtube.com/channel/UCC-lyoTfSrcJzA1ab3APAgw). Also follow core maintainers **Nuno Campos** (`nfcampos`) and **Will Hinthorn** (`hinthornw`). | **A1** | High-value source for runtime architecture rather than prompt recipes. Promote maintainers and design documents, not every LangChain marketing item. |
| **4** | **Xingyao Wang / OpenHands** — X [@xingyaow_](https://x.com/xingyaow_), [GitHub](https://github.com/xingyaoww) | Co-founder and major technical voice behind [OpenHands](https://github.com/OpenHands/OpenHands), with ~80k GitHub stars in the snapshot. Focuses on software-agent infrastructure, verification stacks, SDKs, benchmarks, and production scaling. [Official YouTube](https://www.youtube.com/channel/UCHWq8J_nvQqugBTH19ILCEA); [OpenHands paper](https://arxiv.org/abs/2407.16741). | **A1** | Strongest open implementation/production/research intersection for general software agents. |
| **5** | **Paul Gauthier / Aider** — [GitHub](https://github.com/paul-gauthier), [Aider repository](https://github.com/Aider-AI/aider), [leaderboards](https://aider.chat/docs/leaderboards/) | Primary maintainer of a mature terminal coding agent; ~47k stars. Publishes model-specific coding benchmarks and ships frequent implementation changes. | **A1** | Unusually valuable longitudinal source: implementation choices, model regressions, edit formats, repository maps, and benchmark movement can be tracked together. |
| **6** | **Omar Khattab / DSPy** — X [@lateinteraction](https://x.com/lateinteraction), [GitHub](https://github.com/okhat), [site](https://omarkhattab.com/) | Creator of [DSPy](https://github.com/stanfordnlp/dspy), ~36k stars, and associated research on compiling/optimizing declarative LM programs: [paper](https://arxiv.org/abs/2310.03714). | **A1** | Adds optimization and systematic program construction, preventing the corpus from reducing agent engineering to hand-authored prompts and orchestration diagrams. |
| **7** | **John Yang, Kilian Lieret, and the SWE-agent team** — X [@jyangballin](https://x.com/jyangballin), [@klieret](https://x.com/klieret); GitHub [john-b-yang](https://github.com/john-b-yang), [klieret](https://github.com/klieret) | [SWE-agent](https://github.com/SWE-agent/SWE-agent), ~20k stars; NeurIPS 2024 work on agent-computer interfaces: [paper](https://arxiv.org/abs/2405.15793). | **A1** | Foundational evidence on how the interface/harness changes coding-agent performance. Track papers, benchmark methodology, and code—not only benchmark scores. |
| **8** | **Chi Wang and Eric Zhu / AG2–AutoGen lineage** — X [@Chi_Wang_](https://x.com/Chi_Wang_), [@ekzhu](https://x.com/ekzhu); GitHub [sonichi](https://github.com/sonichi), [ekzhu](https://github.com/ekzhu) | Original [AutoGen paper](https://arxiv.org/abs/2308.08155); current Microsoft repository [microsoft/autogen](https://github.com/microsoft/autogen), ~60k stars; creator-led continuation [ag2ai/ag2](https://github.com/ag2ai/ag2). | **A1** | Key multi-agent lineage. Preserve the **organizational fork/evolution**: Microsoft AutoGen and creator-led AG2 should be separate nodes, not collapsed into one source. |
| **9** | **Samuel Colvin and Douwe Maan / PydanticAI** — X [@samuelcolvin](https://x.com/samuelcolvin); GitHub [samuelcolvin](https://github.com/samuelcolvin), [DouweM](https://github.com/DouweM) | [PydanticAI](https://github.com/pydantic/pydantic-ai), ~19k stars; typed outputs, dependency injection, tool execution, testing, and observability through the broader Pydantic stack. Douwe identifies as PydanticAI lead. | **A2** | High-signal Python engineering source with stronger typing/testing discipline than most agent tutorial ecosystems. |
| **10** | **Dex Horthy / HumanLayer** — X [@dexhorthy](https://x.com/dexhorthy), [GitHub](https://github.com/dexhorthy), [HumanLayer](https://github.com/humanlayer/humanlayer) | Maintains an active coding-agent system (~11k stars) and publishes concrete practices around context engineering, approvals, human proof, complex codebases, and “12-factor agents.” | **A2** | Strong operator perspective on making coding agents useful in consequential, long-lived repositories rather than toy greenfield demos. |
| **11** | **Jason Liu / Instructor** — X [@jxnlco](https://x.com/jxnlco), [GitHub](https://github.com/jxnl), [site](https://jxnl.co/), [Instructor](https://github.com/567-labs/instructor) | Maintains structured-output infrastructure (~14k stars) and discusses production failure edges, schemas, memory, skills, and practical agent workflows. | **A2** | Structured outputs are a foundational reliability primitive. Promote implementation/release notes and technical essays; down-rank casual social experimentation. |
| **12** | **Jerry Liu / LlamaIndex** — [GitHub](https://github.com/jerryjliu), [LlamaIndex](https://github.com/run-llama/llama_index) | Major document-agent/RAG platform, ~51k stars, with extensive examples and [official YouTube](https://www.youtube.com/channel/UCeRjipR4_SsCddq9VZ2AeKg). | **A2** | Important for document agents, retrieval, workflows, and data-backed production systems. Filter aggressively because the education volume is high and uneven. |
| **13** | **João Moura / CrewAI** — X [@joaomdmoura](https://x.com/joaomdmoura), [GitHub](https://github.com/joaomdmoura), [CrewAI](https://github.com/crewAIInc/crewAI) | Founder and top contributor to a widely adopted multi-agent framework (~56k stars). [Official YouTube](https://www.youtube.com/channel/UCTulKzR5sldNq2eAxxdaEuA). | **A2** | Useful implementation and adoption signal for role-based multi-agent systems. Require code, benchmarks, or postmortems before promoting conceptual “crew” content. |
| **14** | **Abhi Aiyer / Mastra** — X [@AbhiAiyer](https://x.com/AbhiAiyer), [GitHub](https://github.com/abhiaiyer91), [Mastra](https://github.com/mastra-ai/mastra) | CTO and leading contributor to a TypeScript agent/application framework (~26k stars) covering workflows, evals, memory, and integrations. | **A2** | Corrects Python-heavy corpus bias and supplies production-oriented TypeScript/JavaScript architecture. |
| **15** | **Eugene Yan** — X [@eugeneyan](https://x.com/eugeneyan), [GitHub](https://github.com/eugeneyan), [site](https://eugeneyan.com/) | Applied ML/LLM engineering essays, patterns, evaluation, product design, and production lessons; [LLM application patterns](https://eugeneyan.com/writing/llm-patterns/). | **B1** | Valuable operator synthesis and architectural framing. Use as a synthesis node, with underlying examples separately sourced. |

---

## Institutional and primary-source layer

These should be ingested as **source nodes**, not treated as individual creators:

1. **OpenAI Agents SDK** — [repository](https://github.com/openai/openai-agents-python), [documentation](https://openai.github.io/openai-agents-python/) — ~28k stars.
2. **Google Agent Development Kit** — [repository](https://github.com/google/adk-python), [documentation](https://google.github.io/adk-docs/) — ~21k stars.
3. **Model Context Protocol** — [specification repository](https://github.com/modelcontextprotocol/modelcontextprotocol), [canonical site](https://modelcontextprotocol.io/).
4. **Microsoft AutoGen** — [repository](https://github.com/microsoft/autogen) — track separately from AG2.
5. **Vercel AI SDK** — [repository](https://github.com/vercel/ai), [documentation](https://ai-sdk.dev/) — important TypeScript agent/runtime source.
6. **Temporal** — [Python SDK](https://github.com/temporalio/sdk-python) and durable-execution material — useful when agent workflows become long-running production processes.
7. **Observability/evaluation implementations:** [Langfuse](https://github.com/langfuse/langfuse), [Arize Phoenix](https://github.com/Arize-ai/phoenix), [DeepEval](https://github.com/confident-ai/deepeval), and [OpenEvals](https://github.com/langchain-ai/openevals).

### Benchmark/research nodes to connect

- **SWE-bench / SWE-agent** — coding-agent task performance.
- **OpenHands** — software-agent platform and evaluation.
- **DSPy** — prompt/program optimization.
- **AutoGen paper** — multi-agent conversation architecture.
- Add **OSWorld, WebArena, GAIA, τ-bench, and Terminal-Bench** as benchmark entities, with version/date/model/harness fields. Never ingest leaderboard scores without the harness and benchmark version.

---

## Recommended graph structure

```text
IndyDevDan ─┬─ coding-agent workflows ─ Simon Willison ─ Aider ─ SWE-agent
            ├─ orchestration ─ LangGraph ─ AutoGen/AG2 ─ CrewAI
            └─ implementation education ─ PydanticAI ─ Mastra

Nick Saraev ─┬─ automation/operators ─ HumanLayer ─ Temporal
             └─ multi-agent use cases ─ CrewAI ─ LlamaIndex

Reliability spine:
Hamel Husain ↔ Shreya Shankar
      ├─ eval design ─ Langfuse / Phoenix / DeepEval / OpenEvals
      ├─ trace analysis ─ LangSmith / OpenTelemetry
      └─ structured contracts ─ Instructor / PydanticAI

Research spine:
Omar Khattab / DSPy
      ├─ optimization
      ├─ SWE-agent
      ├─ OpenHands
      └─ benchmark nodes

Standards spine:
MCP ↔ OpenAI Agents SDK ↔ Google ADK ↔ Vercel AI SDK
```

## Promotion policy

Promote a creator/source into the core corpus only after one of these gates:

- **Builder gate:** maintained repository plus meaningful technical releases.
- **Research gate:** paper plus code/data or reproducible benchmark.
- **Operator gate:** production traces, postmortems, failure analysis, or eval artifacts.
- **Educator gate:** tutorial points to inspectable code and distinguishes demo patterns from production guidance.

Down-rank:

- Framework comparisons without runnable methodology.
- Benchmark screenshots without model, harness, task set, and date.
- “Agent swarms” or revenue claims with no implementation evidence.
- Sponsored tutorials that omit operational failures.
- X threads whose claims cannot be traced to code, papers, demonstrations, or production evidence.

## Recommended first ingestion wave

1. Simon Willison  
2. Hamel Husain + Shreya Shankar  
3. Harrison Chase plus core LangGraph maintainers  
4. Xingyao Wang / OpenHands  
5. Paul Gauthier / Aider  
6. Omar Khattab / DSPy  
7. SWE-agent team  
8. Chi Wang + Eric Zhu, with AutoGen/AG2 lineage preserved  
9. PydanticAI leads  
10. Dex Horthy / HumanLayer  

**Files created or modified:** none.  
**Issue encountered:** general web search was unavailable because Firecrawl lacked credits/configuration. I used live GitHub API data, X search, arXiv endpoints, and public YouTube metadata instead; no authenticated personal accounts or repository writes were used.