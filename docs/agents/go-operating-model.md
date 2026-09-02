---
status: living
audience:
  - builder
  - reviewer
---

# Go operating model

This is the Go translation of the [domain-first operating model](domain-first-operating-model.md). Eval Lab application code remains Python-only; use these rules for adjacent Go services and tools.

## Model first

Start with the states the program is allowed to hold, not handlers, validators, or interfaces.

- Give domain concepts named types instead of passing `string`, `map[string]any`, or unrelated booleans.
- Keep fields unexported when callers must use a constructor or parser to establish invariants.
- Put values that must occur together in one struct. Do not represent a request as an optional callback plus a separate count or enable flag.
- Use separate structs for distinct outcomes. A parsed record and an unreadable record should not share one struct with `Valid bool` and conditionally meaningful pointers.
- Model state transitions as functions from one valid state to another. Do not expose setters that permit partial transitions.

## Parse at the edge

Parsing changes the type. Validation that returns only an error leaves callers holding the same weak value.

```go
type Spec struct {
    purpose Purpose
}

func ParseSpec(data []byte) (Spec, error) {
    // Decode, check the complete boundary contract, and return Spec only on success.
}
```

After `ParseSpec` succeeds, core code accepts `Spec`, not `[]byte`, `map[string]any`, or a second `Validate()` call.

- Decode JSON with unknown fields rejected when the file is a closed contract.
- Parse CLI bounds in the flag or command layer before constructing a domain request.
- Wrap errors at the boundary with the field, file, command, or external operation that failed.
- Preserve an external parse failure as its own reporting type when it must remain visible.
- Never use zero values as silent substitutes for missing required domain facts.

## Closed alternatives

For a closed set of outcomes, use an interface with an unexported marker and concrete structs in the owning package, or keep the branch local and exhaustive.

```go
type LoadResult interface{ loadResult() }

type Loaded struct{ Spec Spec }
type Unreadable struct{ Err error }

func (Loaded) loadResult()     {}
func (Unreadable) loadResult() {}
```

A type switch over that set is preferable to a record containing `OK bool`, `Spec *Spec`, and `Err error`, which permits neither, both, and mismatched combinations.

## Interfaces and test doubles

Accept an interface at the narrow boundary that consumes it; return concrete domain types.

Create an interface only when:

1. there are multiple production implementations; or
2. it isolates a real external boundary such as a process, clock, filesystem, network client, or database.

Do not create interfaces to mock pure code. Do not ship an in-memory store, canned provider, fake success path, or compatibility adapter solely for tests. Test-local fakes implement the production boundary interface from `_test.go`.

## Error handling

- Return errors; do not encode failure as an empty domain object.
- Handle an error where the program can add context or choose a real recovery path.
- Do not catch an error only to continue with fabricated success.
- Use `errors.Is` and `errors.As` for semantic decisions; do not branch on error strings.
- Panic only for violated internal invariants that parsed inputs cannot trigger.

## Tests

A Go test must protect observable behavior, a domain invariant, a state transition, precedence, or a real failure mode.

Delete tests that:

- read `.go` source to assert imports, strings, call order, or function counts;
- duplicate a registry or command tree in a golden file;
- assert private wiring or the existence of a mock seam;
- compare a forwarding wrapper with the function it forwards to;
- construct values callers cannot obtain through the package API;
- assert canned production fallback output.

Prefer table tests for meaningful input classes, fuzz tests for parsers and serialization boundaries, and integration tests at real process or storage boundaries. A golden file is warranted only when the exact serialized artifact is a public contract.

## Simplification pass

After strengthening a type or parser:

1. migrate every caller;
2. remove downstream checks made unreachable;
3. remove obsolete options, shims, wrappers, and aliases;
4. delete tests whose only subject disappeared;
5. run the behavioral scenario through the real public surface.

Do not leave parallel old and new paths. The simpler state space is the deliverable.
