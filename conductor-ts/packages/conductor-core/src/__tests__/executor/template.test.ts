/**
 * Ports tests/test_executor/test_template.py to TypeScript / vitest.
 */
import { describe, expect, it } from "vitest";
import { TemplateError } from "../../exceptions.js";
import { TemplateRenderer } from "../../executor/template.js";

describe("TemplateRenderer basics", () => {
  it("renders a simple variable substitution", () => {
    const r = new TemplateRenderer();
    expect(r.render("Hello {{ name }}!", { name: "World" })).toBe("Hello World!");
  });

  it("renders multiple variables", () => {
    const r = new TemplateRenderer();
    expect(r.render("{{ greeting }}, {{ name }}!", { greeting: "Hello", name: "World" })).toBe(
      "Hello, World!",
    );
  });

  it("preserves trailing newline", () => {
    const r = new TemplateRenderer();
    expect(r.render("Hello {{ name }}!\n", { name: "World" })).toBe("Hello World!\n");
  });

  it("renders template with no variables unchanged", () => {
    const r = new TemplateRenderer();
    expect(r.render("Hello, World!", {})).toBe("Hello, World!");
  });
});

describe("TemplateRenderer json filter", () => {
  it("serializes a list to JSON", () => {
    const r = new TemplateRenderer();
    const result = r.render("{{ items | json }}", { items: ["a", "b", "c"] });
    expect(result).toContain('"a"');
    expect(result).toContain('"b"');
    expect(result).toContain('"c"');
  });

  it("serializes a dict to JSON", () => {
    const r = new TemplateRenderer();
    const result = r.render("{{ data | json }}", {
      data: { key: "value", number: 42 },
    });
    expect(result).toContain('"key": "value"');
    expect(result).toContain('"number": 42');
  });

  it("serializes nested objects to JSON", () => {
    const r = new TemplateRenderer();
    const result = r.render("{{ data | json }}", {
      data: { nested: { deep: "value" } },
    });
    expect(result).toContain('"nested"');
    expect(result).toContain('"deep": "value"');
  });
});

describe("TemplateRenderer default filter", () => {
  it("returns default when value is null/undefined", () => {
    const r = new TemplateRenderer();
    const result = r.render("Value: {{ value | default('fallback') }}", {
      value: null,
    });
    expect(result).toBe("Value: fallback");
  });

  it("returns actual value when not null", () => {
    const r = new TemplateRenderer();
    const result = r.render("Value: {{ value | default('fallback') }}", {
      value: "actual",
    });
    expect(result).toBe("Value: actual");
  });
});

describe("TemplateRenderer conditionals", () => {
  it("if block when condition is true", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% if approved %}Approved{% else %}Rejected{% endif %}",
      { approved: true },
    );
    expect(result).toBe("Approved");
  });

  it("if block when condition is false", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% if approved %}Approved{% else %}Rejected{% endif %}",
      { approved: false },
    );
    expect(result).toBe("Rejected");
  });

  it("if block with comparison operators", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% if score > 5 %}Pass{% else %}Fail{% endif %}",
      { score: 7 },
    );
    expect(result).toBe("Pass");
  });

  it("if block with nested attribute access", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% if output.status == 'ok' %}Success{% endif %}",
      { output: { status: "ok" } },
    );
    expect(result).toBe("Success");
  });
});

describe("TemplateRenderer loops", () => {
  it("for loop over a list", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% for item in items %}{{ item }} {% endfor %}",
      { items: ["a", "b", "c"] },
    );
    expect(result).toBe("a b c ");
  });

  it("for loop with loop.index", () => {
    const r = new TemplateRenderer();
    const result = r.render(
      "{% for item in items %}{{ loop.index }}.{{ item }} {% endfor %}",
      { items: ["a", "b"] },
    );
    expect(result).toBe("1.a 2.b ");
  });
});

describe("TemplateRenderer missing variables", () => {
  it("missing variable raises TemplateError", () => {
    const r = new TemplateRenderer();
    expect(() => r.render("Hello {{ missing_var }}!", {})).toThrow(TemplateError);
  });

  it("nested missing variable raises TemplateError", () => {
    const r = new TemplateRenderer();
    expect(() => r.render("{{ obj.missing }}", {})).toThrow(TemplateError);
  });
});

describe("TemplateRenderer nested access", () => {
  it("accesses nested dict values", () => {
    const r = new TemplateRenderer();
    const result = r.render("{{ user.name }}", { user: { name: "Alice" } });
    expect(result).toBe("Alice");
  });

  it("accesses deep nested values", () => {
    const r = new TemplateRenderer();
    const result = r.render("{{ a.b.c }}", { a: { b: { c: "deep" } } });
    expect(result).toBe("deep");
  });
});

describe("TemplateRenderer.renderBool", () => {
  it("evaluates truthy condition as true", () => {
    const r = new TemplateRenderer();
    expect(r.renderBool("{{ approved }}", { approved: true })).toBe(true);
  });

  it("evaluates falsy condition as false", () => {
    const r = new TemplateRenderer();
    expect(r.renderBool("{{ approved }}", { approved: false })).toBe(false);
  });

  it("evaluates comparison condition", () => {
    const r = new TemplateRenderer();
    expect(r.renderBool("{{ score >= 8 }}", { score: 9 })).toBe(true);
    expect(r.renderBool("{{ score >= 8 }}", { score: 7 })).toBe(false);
  });
});
