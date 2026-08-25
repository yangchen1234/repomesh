export async function withExponentialBackoff(operation, options = {}) {
  const attempts = options.attempts ?? 4;
  const baseDelayMs = options.baseDelayMs ?? 100;
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (attempt + 1 === attempts) break;
      const jitter = Math.floor(Math.random() * baseDelayMs);
      const delay = baseDelayMs * 2 ** attempt + jitter;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
  throw lastError;
}
