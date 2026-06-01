# Conductor Workflow Patterns

Copy-paste orchestration shapes in Conductor YAML. Each pattern says when to use
it, then gives runnable YAML. Match the pattern to the Step 2 answers in
`SKILL.md`: known list vs unknown count, one pass vs staged, barrier needed or not.

---

## 1. Fan-out then synthesize (parallel group)

**When:** A known list of independent tasks, one pass each, and you need one
combined answer at the end. The synthesis genuinely needs every result, so the
barrier (parallel group) is correct here.

```yaml
workflow:
  name: research-fanout
  description: Research independent questions in parallel, synthesize one report
  entry_point: research-group
  runtime:
    provider: copilot
    default_model: gpt-4.1

parallel:
  - name: research-group
    agents:
      - researcher-1
      - researcher-2
      - researcher-3
    failure_mode: continue_on_error
    routes:
      - to: synthesizer

agents:
  - name: researcher-1
    prompt: "Research and report verified facts: {{ workflow.input.questions[0] }}"
    output:
      facts:
        type: array
        items: { type: string }
  
  - name: researcher-2
    prompt: "Research and report verified facts: {{ workflow.input.questions[1] }}"
    output:
      facts:
        type: array
        items: { type: string }
  
  - name: researcher-3
    prompt: "Research and report verified facts: {{ workflow.input.questions[2] }}"
    output:
      facts:
        type: array
        items: { type: string }
  
  - name: synthesizer
    prompt: |
      Combine the research below into one cohesive briefing; call out disagreements.
      
      Question 1: {{ research-group.researcher-1.output.facts | tojson }}
      Question 2: {{ research-group.researcher-2.output.facts | tojson }}
      Question 3: {{ research-group.researcher-3.output.facts | tojson }}
    routes:
      - to: $end

output:
  report: "{{ synthesizer.output }}"
```

---

## 2. Fan-out with for_each (dynamic list)

**When:** The list of items is dynamic (passed as input or discovered at runtime).

```yaml
workflow:
  name: review-files
  description: Review each file independently, then aggregate
  entry_point: review-group
  runtime:
    provider: copilot

for_each:
  - name: review-group
    type: for_each
    source: workflow.input.files
    as: item
    agent:
      name: reviewer
      prompt: "Review {{ item }} for bugs and security issues"
      output:
        issues:
          type: array
          items:
            type: object
            properties:
              severity: { type: string }
              description: { type: string }
    max_concurrent: 4
    failure_mode: continue_on_error
    routes:
      - to: aggregator

agents:
  - name: aggregator
    prompt: |
      Aggregate all findings:
      {% for result in review-group.outputs %}
      File: {{ workflow.input.files[loop.index0] }}
      Issues: {{ result.issues | length }}
      {% endfor %}
    routes:
      - to: $end

output:
  total_issues: "{{ review-group.outputs | map(attribute='issues') | sum(start=[]) | length }}"
```

---

## 3. Pipeline: review then verify (sequential routing)

**When:** Items flow through ordered stages and each item should advance the
moment *it* is ready — no waiting for the slowest sibling. This is the default;
prefer it over parallel groups with barriers.

```yaml
workflow:
  name: review-and-verify
  description: Review code, then verify each finding
  entry_point: reviewer
  runtime:
    provider: copilot

agents:
  - name: reviewer
    prompt: "Find logic bugs in the changed files"
    output:
      findings:
        type: array
        items:
          type: object
          properties:
            title: { type: string }
            file: { type: string }
            line: { type: number }
    routes:
      - to: verify-group
        when: "{{ output.findings | length > 0 }}"
      - to: $end

for_each:
  - name: verify-group
    type: for_each
    source: reviewer.output.findings
    as: item
    agent:
      name: verifier
      prompt: "Adversarially verify: {{ item.title }} in {{ item.file }}:{{ item.line }}"
      output:
        is_real: { type: boolean }
        reason: { type: string }
    max_concurrent: 4
    routes:
      - to: $end

output:
  confirmed: "{{ verify-group.outputs | selectattr('is_real') | list }}"
```

---

## 4. Loop until pass (loop-back routing)

**When:** Retry until a condition is met (review → fix → review).

```yaml
workflow:
  name: implement-and-review
  description: Implement, review, fix until review passes
  entry_point: implementer
  runtime:
    provider: copilot
  limits:
    max_iterations: 10

agents:
  - name: implementer
    prompt: "Implement {{ workflow.input.task }}"
    routes:
      - to: reviewer
  
  - name: reviewer
    prompt: "Review the changes for {{ workflow.input.task }}"
    output:
      passed: { type: boolean }
      issues:
        type: array
        items: { type: string }
    routes:
      - to: fixer
        when: "{{ not output.passed }}"
      - to: $end
  
  - name: fixer
    prompt: |
      Fix these review issues:
      {% for issue in reviewer.output.issues %}
      - {{ issue }}
      {% endfor %}
    routes:
      - to: reviewer

output:
  passed: "{{ reviewer.output.passed }}"
  rounds: "{{ context.iteration }}"
```

---

## 5. Loop until target count

**When:** Discovery with a fixed goal — "find 10 bugs". Use iteration limit as
the cap.

```yaml
workflow:
  name: find-bugs
  description: Find bugs until we have 10
  entry_point: finder
  runtime:
    provider: copilot
  limits:
    max_iterations: 20
  context:
    mode: accumulate

agents:
  - name: finder
    prompt: |
      Find bugs not already listed below.
      {% if finder.output %}
      Already found: {{ finder.output.bugs | map(attribute='title') | join(', ') }}
      {% endif %}
    output:
      bugs:
        type: array
        items:
          type: object
          properties:
            title: { type: string }
            file: { type: string }
    routes:
      - to: finder
        when: "{{ (finder.output.bugs | length) < 10 }}"
      - to: $end

output:
  bugs: "{{ finder.output.bugs[:10] }}"
```

---

## 6. Adversarial verification (skeptic vote)

**When:** A finding will be acted on and a plausible-but-wrong one is costly.
Spawn N independent skeptics, each told to *refute*; keep the finding only on a
majority.

```yaml
workflow:
  name: verify-claim
  description: Verify a claim with 3 independent skeptics
  entry_point: skeptic-group
  runtime:
    provider: copilot

parallel:
  - name: skeptic-group
    agents:
      - skeptic-1
      - skeptic-2
      - skeptic-3
    failure_mode: continue_on_error
    routes:
      - to: $end

agents:
  - name: skeptic-1
    prompt: |
      Try hard to REFUTE this claim. Default to refuted=true if uncertain.
      
      Claim: {{ workflow.input.claim }}
    output:
      refuted: { type: boolean }
      reason: { type: string }
  
  - name: skeptic-2
    prompt: |
      Try hard to REFUTE this claim. Default to refuted=true if uncertain.
      
      Claim: {{ workflow.input.claim }}
    output:
      refuted: { type: boolean }
      reason: { type: string }
  
  - name: skeptic-3
    prompt: |
      Try hard to REFUTE this claim. Default to refuted=true if uncertain.
      
      Claim: {{ workflow.input.claim }}
    output:
      refuted: { type: boolean }
      reason: { type: string }

output:
  verified: "{{ [skeptic-group.skeptic-1.output.refuted, skeptic-group.skeptic-2.output.refuted, skeptic-group.skeptic-3.output.refuted] | reject | list | length >= 2 }}"
  votes:
    - "{{ skeptic-group.skeptic-1.output }}"
    - "{{ skeptic-group.skeptic-2.output }}"
    - "{{ skeptic-group.skeptic-3.output }}"
```

---

## 7. Judge panel (N attempts, score, synthesize)

**When:** The solution space is wide and one-attempt-iterated is weak. Generate
independent attempts from different angles, score them, synthesize from the winner.

```yaml
workflow:
  name: judge-panel
  description: Draft plans from multiple angles, score, synthesize best
  entry_point: draft-group
  runtime:
    provider: copilot

parallel:
  - name: draft-group
    agents:
      - drafter-mvp
      - drafter-risk
      - drafter-user
      - drafter-cost
    failure_mode: continue_on_error
    routes:
      - to: judge-group

agents:
  - name: drafter-mvp
    prompt: "Produce a plan for: {{ workflow.input.idea }}. Take a strictly MVP-first approach."
  
  - name: drafter-risk
    prompt: "Produce a plan for: {{ workflow.input.idea }}. Take a strictly risk-first approach."
  
  - name: drafter-user
    prompt: "Produce a plan for: {{ workflow.input.idea }}. Take a strictly user-first approach."
  
  - name: drafter-cost
    prompt: "Produce a plan for: {{ workflow.input.idea }}. Take a strictly cost-first approach."

parallel:
  - name: judge-group
    agents:
      - judge-mvp
      - judge-risk
      - judge-user
      - judge-cost
    failure_mode: continue_on_error
    routes:
      - to: synthesizer

agents:
  - name: judge-mvp
    prompt: |
      Score this plan 1-10 for feasibility and impact.
      {{ draft-group.drafter-mvp.output }}
    output:
      score: { type: number }
      why: { type: string }
  
  - name: judge-risk
    prompt: |
      Score this plan 1-10 for feasibility and impact.
      {{ draft-group.drafter-risk.output }}
    output:
      score: { type: number }
      why: { type: string }
  
  - name: judge-user
    prompt: |
      Score this plan 1-10 for feasibility and impact.
      {{ draft-group.drafter-user.output }}
    output:
      score: { type: number }
      why: { type: string }
  
  - name: judge-cost
    prompt: |
      Score this plan 1-10 for feasibility and impact.
      {{ draft-group.drafter-cost.output }}
    output:
      score: { type: number }
      why: { type: string }
  
  - name: synthesizer
    prompt: |
      Write the definitive plan. Base it on the WINNER, grafting the best ideas
      from the runners-up.
      
      WINNER (score {{ [judge-group.judge-mvp.output.score, judge-group.judge-risk.output.score, judge-group.judge-user.output.score, judge-group.judge-cost.output.score] | max }}):
      {% set scores = [
        (judge-group.judge-mvp.output.score, draft-group.drafter-mvp.output),
        (judge-group.judge-risk.output.score, draft-group.drafter-risk.output),
        (judge-group.judge-user.output.score, draft-group.drafter-user.output),
        (judge-group.judge-cost.output.score, draft-group.drafter-cost.output)
      ] | sort(reverse=true) %}
      {{ scores[0][1] }}
      
      RUNNERS-UP:
      {% for score, draft in scores[1:] %}
      Score {{ score }}: {{ draft }}
      {% endfor %}
    routes:
      - to: $end

output:
  final_plan: "{{ synthesizer.output }}"
```

---

## 8. Sub-workflow composition

**When:** A big workflow has a self-contained sub-job that is itself a workflow.

```yaml
workflow:
  name: article-pipeline
  description: Research, then write article
  entry_point: research-sub
  runtime:
    provider: copilot

agents:
  - name: research-sub
    type: workflow
    workflow: ./research-fanout.yaml
    input_mapping:
      questions: "{{ workflow.input.questions }}"
    routes:
      - to: writer
  
  - name: writer
    prompt: |
      Write an article from this research:
      {{ research-sub.output.report }}
    routes:
      - to: $end

output:
  article: "{{ writer.output }}"
```

---

## 9. Human gate with conditional routing

**When:** You need human approval before proceeding, with different paths based
on the decision.

```yaml
workflow:
  name: approval-workflow
  description: Propose changes, get approval, implement or revise
  entry_point: proposer
  runtime:
    provider: copilot

agents:
  - name: proposer
    prompt: "Propose changes for {{ workflow.input.task }}"
    routes:
      - to: approval-gate
  
  - name: approval-gate
    type: human_gate
    prompt: |
      # Proposed Changes
      
      {{ proposer.output }}
      
      Please review and decide:
    options:
      - value: approve
        prompt_for:
          priority:
            type: string
            description: "Priority level (high/medium/low)"
        route: implementer
      - value: revise
        prompt_for:
          feedback:
            type: string
            description: "What needs to change?"
        route: reviser
      - value: reject
        route: $end
  
  - name: implementer
    prompt: |
      Implement these changes (priority: {{ approval-gate.additional_input.priority }}):
      {{ proposer.output }}
    routes:
      - to: $end
  
  - name: reviser
    prompt: |
      Revise the proposal based on this feedback:
      {{ approval-gate.additional_input.feedback }}
      
      Original proposal:
      {{ proposer.output }}
    routes:
      - to: approval-gate

output:
  decision: "{{ approval-gate.selected }}"
  result: "{{ implementer.output if approval-gate.selected == 'approve' else 'rejected' }}"
```

---

## 10. Script step with conditional routing

**When:** You need to run a shell command and route based on its output.

```yaml
workflow:
  name: test-and-fix
  description: Run tests, fix failures, repeat
  entry_point: test-runner
  runtime:
    provider: copilot
  limits:
    max_iterations: 5

agents:
  - name: test-runner
    type: script
    command: npm test
    output:
      exit_code: { type: number }
      stdout: { type: string }
      stderr: { type: string }
    routes:
      - to: $end
        when: "{{ output.exit_code == 0 }}"
      - to: fixer
  
  - name: fixer
    prompt: |
      Fix these test failures:
      {{ test-runner.output.stderr }}
    routes:
      - to: test-runner

output:
  passed: "{{ test-runner.output.exit_code == 0 }}"
  attempts: "{{ context.iteration }}"
```

---

## Common Jinja2 patterns

### Filter lists
```yaml
# Get only passed items
"{{ results | selectattr('passed') | list }}"

# Count items
"{{ results | length }}"

# Get first N items
"{{ results[:10] }}"

# Map to attribute
"{{ results | map(attribute='title') | list }}"

# Join strings
"{{ items | join(', ') }}"
```

### Conditional output
```yaml
"{{ 'passed' if output.passed else 'failed' }}"
```

### Loop in prompts
```yaml
prompt: |
  {% for item in previous_agent.output.items %}
  - {{ item.title }}: {{ item.description }}
  {% endfor %}
```

### Check if empty
```yaml
when: "{{ output.issues | length > 0 }}"
when: "{{ not output.passed }}"
```
