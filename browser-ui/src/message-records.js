export class MessageRecords {
  constructor(settleMilliseconds = 120) {
    this.committed = [];
    this.live = [];
    this.nextId = 1;
    this.startRow = 0;
    this.settleMilliseconds = settleMilliseconds;
    this.pendingFreeze = undefined;
    this.heldUser = undefined;
  }

  update(candidates, freezeBefore, now = Date.now()) {
    if (this.heldUser) {
      const heldUser = this.heldUser;
      const stillPainted = candidates.some(
        (candidate) => candidate.kind === "user"
          && candidate.start === heldUser.start
          && candidate.source === heldUser.source,
      );
      if (stillPainted) {
        candidates = candidates.filter(
          (candidate) => !(candidate.kind === "user"
            && candidate.start === heldUser.start
            && candidate.source === heldUser.source),
        );
      } else {
        this.heldUser = undefined;
      }
    }
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

    // A submitted composer is already complete. Codex can leave this user
    // panel on screen for only a few milliseconds before reusing the same rows
    // for its empty composer and response, so the general settle window is too
    // slow here. Commit the ordered prefix immediately, but stop the row
    // watermark at the prompt's first row because later output may reuse it.
    const submittedUser = candidates.findLastIndex(
      (candidate) => candidate.kind === "user" && candidate.messageRole === "user",
    );
    if (submittedUser >= 0) {
      const safeStartRow = candidates[submittedUser].start;
      const newlyCommitted = this.live.splice(0, submittedUser + 1);
      for (let index = 0; index < newlyCommitted.length; index += 1) {
        const record = newlyCommitted[index];
        this.assign(record, candidates[index], true);
        this.startRow = Math.max(
          this.startRow,
          Math.min(record.end + 1, safeStartRow),
        );
        this.committed.push(record);
      }
      this.heldUser = {
        start: candidates[submittedUser].start,
        source: candidates[submittedUser].source,
      };
      this.pendingFreeze = undefined;
      return [...this.committed, ...this.live];
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
