function canonicalUserText(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/^\s*[›❯]\s?/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
}

function userTextMatches(authoritative, terminal) {
  const left = canonicalUserText(authoritative);
  const right = canonicalUserText(terminal);
  if (!left || !right) return false;
  if (left === right) return true;
  return Math.min(left.length, right.length) >= 12
    && (left.includes(right) || right.includes(left));
}

function turnKey(turn) {
  return `${turn.sessionId}:${turn.id}`;
}

function alignUsers(turns, records) {
  const users = records
    .map((record, index) => ({ record, index }))
    .filter(({ record }) => record.kind === "user" && record.messageRole === "user");
  const rows = turns.length + 1;
  const columns = users.length + 1;
  const lengths = Array.from({ length: rows }, () => new Uint32Array(columns));

  for (let turnIndex = turns.length - 1; turnIndex >= 0; turnIndex -= 1) {
    for (let userIndex = users.length - 1; userIndex >= 0; userIndex -= 1) {
      if (userTextMatches(turns[turnIndex].userMarkdown, users[userIndex].record.source)) {
        lengths[turnIndex][userIndex] = lengths[turnIndex + 1][userIndex + 1] + 1;
      } else {
        lengths[turnIndex][userIndex] = Math.max(
          lengths[turnIndex + 1][userIndex],
          lengths[turnIndex][userIndex + 1],
        );
      }
    }
  }

  const matches = new Map();
  let turnIndex = 0;
  let userIndex = 0;
  while (turnIndex < turns.length && userIndex < users.length) {
    if (userTextMatches(turns[turnIndex].userMarkdown, users[userIndex].record.source)
      && lengths[turnIndex][userIndex]
        === lengths[turnIndex + 1][userIndex + 1] + 1) {
      matches.set(turnKey(turns[turnIndex]), users[userIndex].index);
      turnIndex += 1;
      userIndex += 1;
    } else if (lengths[turnIndex + 1][userIndex]
      >= lengths[turnIndex][userIndex + 1]) {
      turnIndex += 1;
    } else {
      userIndex += 1;
    }
  }
  return matches;
}

function partCoordinates(event) {
  return [
    Number.isInteger(event.output_index) ? event.output_index : 0,
    Number.isInteger(event.content_index) ? event.content_index : 0,
  ];
}

function partKey(event) {
  return partCoordinates(event).join(":");
}

function callMarkdown(call) {
  return [...call.parts.values()]
    .sort((left, right) => (
      left.outputIndex - right.outputIndex
      || left.contentIndex - right.contentIndex
      || left.order - right.order
    ))
    .map((part) => part.done ?? part.delta)
    .filter((source) => source)
    .join("\n\n");
}

function nextMatchedUser(turns, matches, start, fallback) {
  for (let index = start + 1; index < turns.length; index += 1) {
    const match = matches.get(turnKey(turns[index]));
    if (match !== undefined) return match;
  }
  return fallback;
}

function isAbandonedDuplicate(turn, nextTurn) {
  if (!turn.completed || !nextTurn) return false;
  if (turn.sessionId !== nextTurn.sessionId
    || turn.provider !== nextTurn.provider
    || turn.userMarkdown !== nextTurn.userMarkdown) {
    return false;
  }
  return turn.calls.length > 0 && turn.calls.every((call) => (
    !call.completed && !callMarkdown(call)
  ));
}

function displayTurns(turns) {
  return turns.filter((turn, index) => !isAbandonedDuplicate(turn, turns[index + 1]));
}

function trailingUiBoundary(records, start, end) {
  for (let index = start; index < end; index += 1) {
    const record = records[index];
    if (record.kind === "terminal" && (record.panel || record.messageRole === "user")) {
      return index;
    }
  }
  return end;
}

function apiRecord(base, turn, role, source, call, synthetic = false) {
  const start = base?.start ?? 0;
  const end = synthetic ? start : (base?.end ?? start);
  const completed = role === "user" || Boolean(call?.completed || call?.failed);
  return {
    key: call
      ? `api:${turn.sessionId}:${turn.id}:assistant:${call.id}`
      : `api:${turn.sessionId}:${turn.id}:user`,
    kind: role,
    messageRole: role,
    source,
    start,
    end,
    active: !completed,
    frozen: completed,
    background: role === "user" && base?.kind === "user" ? base.background : undefined,
    authoritative: true,
    apiSessionId: turn.sessionId,
    apiTurnId: turn.id,
    apiCallId: call?.id,
    apiProvider: turn.provider,
    signature: [
      "api",
      role,
      call?.revision ?? 0,
      Number(completed),
      source,
    ].join(":"),
  };
}

export class ApiTranscript {
  constructor() {
    this.turns = [];
    this.turnsByKey = new Map();
    this.events = [];
    this.seenEventIds = new Set();
    this.partOrder = 0;
  }

  accept(event, eventId = "") {
    if (!event || event.version !== 1 || typeof event.type !== "string") return false;
    if (eventId && this.seenEventIds.has(eventId)) return false;
    if (eventId) this.seenEventIds.add(eventId);
    this.events.push({ id: eventId || undefined, event: structuredClone(event) });

    const turn = this.#turn(event);
    if (!turn) return true;
    turn.revision += 1;
    const call = event.call_id ? this.#call(turn, event.call_id) : undefined;

    switch (event.type) {
      case "turn.started":
        turn.provider = event.provider;
        turn.ordinal = event.ordinal;
        turn.userMarkdown = event.user?.markdown;
        break;
      case "assistant.delta": {
        if (!call || typeof event.delta !== "string") break;
        const part = this.#part(call, event);
        part.delta += event.delta;
        call.revision += 1;
        break;
      }
      case "assistant.part.done": {
        if (!call || typeof event.markdown !== "string") break;
        const part = this.#part(call, event);
        part.done = event.markdown;
        call.revision += 1;
        break;
      }
      case "call.completed":
        if (call) {
          call.completed = true;
          call.status = event.status;
          call.revision += 1;
        }
        break;
      case "call.failed":
        if (call) {
          call.failed = true;
          call.status = event.error || event.upstream_status || "failed";
          call.revision += 1;
        }
        break;
      case "turn.completed":
        turn.completed = true;
        break;
      default:
        break;
    }
    return true;
  }

  reconcile(records) {
    const turns = displayTurns(
      this.turns.filter((turn) => typeof turn.userMarkdown === "string"),
    );
    if (!turns.length) return records;

    const matches = alignUsers(turns, records);
    const before = Array.from({ length: records.length + 1 }, () => []);
    const replacements = new Map();
    const claimedAssistants = new Set();
    let floor = 0;

    turns.forEach((turn, turnIndex) => {
      const userIndex = matches.get(turnKey(turn));
      const upper = nextMatchedUser(turns, matches, turnIndex, records.length);
      const lower = Math.max(floor, userIndex === undefined ? floor : userIndex + 1);
      const assistantIndices = [];
      for (let index = lower; index < upper; index += 1) {
        if (records[index].kind === "assistant" && !claimedAssistants.has(index)) {
          assistantIndices.push(index);
        }
      }

      const calls = turn.calls
        .map((call) => ({ call, source: callMarkdown(call) }))
        .filter(({ source }) => source);
      const tailUnmatched = userIndex === undefined && turnIndex === turns.length - 1;
      const callAssistantIndices = tailUnmatched
        ? (calls.length ? assistantIndices.slice(-calls.length) : [])
        : assistantIndices;
      const boundary = trailingUiBoundary(records, lower, upper);
      const insertion = callAssistantIndices[0] ?? boundary;
      if (userIndex === undefined) {
        const base = records[insertion] ?? records[Math.max(0, insertion - 1)];
        before[insertion].push(apiRecord(
          base,
          turn,
          "user",
          turn.userMarkdown,
          undefined,
          true,
        ));
      } else {
        replacements.set(
          userIndex,
          apiRecord(records[userIndex], turn, "user", turn.userMarkdown),
        );
      }

      // A submitted TUI panel can disappear for one repaint and then return.
      // The PTY recorder may consequently preserve more than one copy of the
      // same prompt. The API turn owns this user message, so consume every
      // additional matching PTY copy before the next authoritative turn.
      const duplicateStart = userIndex === undefined ? lower : userIndex + 1;
      for (let index = duplicateStart; index < upper; index += 1) {
        const record = records[index];
        if (record.kind === "user"
          && record.messageRole === "user"
          && userTextMatches(turn.userMarkdown, record.source)) {
          replacements.set(index, null);
        }
      }

      let lastClaimed = userIndex;
      calls.forEach(({ call, source }, callIndex) => {
        const recordIndex = callAssistantIndices[callIndex];
        if (recordIndex === undefined) {
          const base = records[boundary]
            ?? records[userIndex]
            ?? records[Math.max(0, boundary - 1)];
          before[boundary].push(apiRecord(base, turn, "assistant", source, call, true));
        } else {
          claimedAssistants.add(recordIndex);
          replacements.set(
            recordIndex,
            apiRecord(records[recordIndex], turn, "assistant", source, call),
          );
          lastClaimed = recordIndex;
        }
      });

      const settled = turn.calls.length > 0
        && turn.calls.every((call) => call.completed || call.failed);
      const nextTurnIsMatched = turnIndex + 1 >= turns.length
        || matches.has(turnKey(turns[turnIndex + 1]));
      if (calls.length && settled && nextTurnIsMatched && !tailUnmatched) {
        for (const recordIndex of assistantIndices.slice(calls.length)) {
          replacements.set(recordIndex, null);
        }
      }

      floor = Math.max(floor, (lastClaimed ?? userIndex ?? insertion - 1) + 1);
    });

    const reconciled = [];
    for (let index = 0; index <= records.length; index += 1) {
      reconciled.push(...before[index]);
      if (index === records.length) break;
      if (replacements.has(index)) {
        const replacement = replacements.get(index);
        if (replacement) reconciled.push(replacement);
      } else {
        reconciled.push(records[index]);
      }
    }
    return reconciled;
  }

  diagnostics() {
    return {
      version: 1,
      events: structuredClone(this.events),
      turns: this.turns.map((turn) => ({
        id: turn.id,
        session_id: turn.sessionId,
        provider: turn.provider,
        ordinal: turn.ordinal,
        user_markdown: turn.userMarkdown,
        completed: turn.completed,
        calls: turn.calls.map((call) => ({
          id: call.id,
          markdown: callMarkdown(call),
          completed: call.completed,
          failed: call.failed,
          status: call.status,
        })),
      })),
    };
  }

  #turn(event) {
    if (!event.turn_id) return undefined;
    const sessionId = event.session_id || "unknown-session";
    const key = `${sessionId}:${event.turn_id}`;
    let turn = this.turnsByKey.get(key);
    if (!turn) {
      turn = {
        id: event.turn_id,
        sessionId,
        provider: event.provider,
        ordinal: event.ordinal,
        userMarkdown: undefined,
        calls: [],
        callsById: new Map(),
        completed: false,
        revision: 0,
      };
      this.turnsByKey.set(key, turn);
      this.turns.push(turn);
    }
    return turn;
  }

  #call(turn, callId) {
    let call = turn.callsById.get(callId);
    if (!call) {
      call = {
        id: callId,
        parts: new Map(),
        completed: false,
        failed: false,
        revision: 0,
      };
      turn.callsById.set(callId, call);
      turn.calls.push(call);
    }
    return call;
  }

  #part(call, event) {
    const key = partKey(event);
    let part = call.parts.get(key);
    if (!part) {
      const [outputIndex, contentIndex] = partCoordinates(event);
      part = {
        outputIndex,
        contentIndex,
        order: this.partOrder,
        delta: "",
        done: undefined,
      };
      this.partOrder += 1;
      call.parts.set(key, part);
    }
    return part;
  }
}
