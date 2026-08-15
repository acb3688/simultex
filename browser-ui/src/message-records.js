export class MessageRecords {
  constructor(settleMilliseconds = 120) {
    this.committed = [];
    this.live = [];
    this.nextId = 1;
    this.startRow = 0;
    this.settleMilliseconds = settleMilliseconds;
    this.pendingFreeze = undefined;
  }

  update(candidates, freezeBefore, now = Date.now()) {
    if (!candidates.length) return [...this.committed, ...this.live];

    // Candidates contain only rows at or after startRow. The shape of this
    // live suffix may grow, shrink, or change kind as Codex repaints its TUI;
    // none of those changes can disturb the committed prefix.
    const sharedLength = Math.min(this.live.length, candidates.length);
    for (let index = 0; index < sharedLength; index += 1) {
      this.assign(this.live[index], candidates[index], false);
    }
    this.live.splice(candidates.length);
    for (let index = this.live.length; index < candidates.length; index += 1) {
      this.live.push(this.create(candidates[index]));
    }

    let freezeCount = Math.min(Math.max(freezeBefore, 0), this.live.length);
    if (freezeCount > 0) {
      const signature = candidates.slice(0, freezeCount)
        .map((candidate) => candidate.signature)
        .join("\u0003");
      if (!this.pendingFreeze
        || this.pendingFreeze.count !== freezeCount
        || this.pendingFreeze.signature !== signature) {
        this.pendingFreeze = { count: freezeCount, signature, since: now };
        freezeCount = 0;
      } else if (now - this.pendingFreeze.since < this.settleMilliseconds) {
        freezeCount = 0;
      }
    } else {
      this.pendingFreeze = undefined;
    }

    const newlyCommitted = this.live.splice(0, freezeCount);
    for (let index = 0; index < newlyCommitted.length; index += 1) {
      const record = newlyCommitted[index];
      this.assign(record, candidates[index], true);
      this.startRow = Math.max(this.startRow, record.end + 1);
      this.committed.push(record);
    }
    if (newlyCommitted.length) this.pendingFreeze = undefined;

    return [...this.committed, ...this.live];
  }

  create(candidate) {
    const record = { key: `message-${this.nextId}` };
    this.nextId += 1;
    this.assign(record, candidate, false);
    return record;
  }

  assign(record, candidate, frozen) {
    const key = record.key;
    Object.assign(record, candidate);
    record.key = key;
    record.frozen = frozen;
    record.signature = `${frozen ? "frozen" : "live"}:${candidate.signature}`;
  }
}
