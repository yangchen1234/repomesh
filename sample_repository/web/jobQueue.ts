export interface Job {
  id: string;
  payload: unknown;
}

export class InMemoryJobQueue {
  private readonly pending: Job[] = [];
  private readonly cancelled = new Set<string>();

  enqueue(job: Job): void {
    if (!this.pending.some((item) => item.id === job.id)) this.pending.push(job);
  }

  cancel(jobId: string): void {
    this.cancelled.add(jobId);
  }

  take(): Job | undefined {
    while (this.pending.length) {
      const job = this.pending.shift()!;
      if (!this.cancelled.has(job.id)) return job;
    }
    return undefined;
  }
}
