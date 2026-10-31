export function tokenBySignalMint<T extends { mint?: string | null }>(
  tokens: T[],
  candidate: { mint: string },
): T | null {
  if (!candidate.mint || !candidate.mint.trim()) return null;
  return tokens.find((token) => token.mint === candidate.mint) ?? null;
}
