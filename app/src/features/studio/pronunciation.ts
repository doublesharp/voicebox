export interface PronunciationRule {
  id: string;
  source: string;
  replacement: string;
  profile_id: string | null;
  case_sensitive: boolean;
  whole_word: boolean;
}

export function pronunciationError(rules: PronunciationRule[]) {
  if (rules.length > 200) return 'Keep the dictionary to 200 rules or fewer.';
  const ids = new Set<string>();
  for (const rule of rules) {
    if (
      !rule.id ||
      ids.has(rule.id) ||
      !rule.source.trim() ||
      !rule.replacement.trim() ||
      [...rule.source].length > 200 ||
      [...rule.replacement].length > 500 ||
      /[\r\n\0]/.test(rule.source + rule.replacement)
    )
      return 'Each rule needs a word or phrase (up to 200 characters) and a spoken alias (up to 500 characters).';
    ids.add(rule.id);
  }
  return null;
}

/** Matches the backend's single-pass, leftmost-longest literal alias semantics. */
export function previewPronunciation(
  text: string,
  rules: PronunciationRule[],
  profileId: string | null,
) {
  const applicable = rules
    .filter((rule) => rule.source && (!rule.profile_id || rule.profile_id === profileId))
    .slice()
    .sort(
      (a, b) =>
        [...b.source].length - [...a.source].length ||
        Number(a.profile_id === null) - Number(b.profile_id === null),
    );
  const input = [...text];
  const output: string[] = [];
  const substitutions: Array<{
    rule_id: string;
    source: string;
    replacement: string;
    start: number;
    end: number;
  }> = [];
  const word = (character: string) => /[\p{L}\p{N}_]/u.test(character);
  let index = 0;
  while (index < input.length) {
    const match = applicable.find((rule) => {
      const end = index + [...rule.source].length;
      if (end > input.length) return false;
      const actual = input.slice(index, end).join('');
      return (
        (rule.case_sensitive
          ? actual === rule.source
          : actual.toLowerCase() === rule.source.toLowerCase()) &&
        (!rule.whole_word ||
          ((index === 0 || !word(input[index - 1])) && (end === input.length || !word(input[end]))))
      );
    });
    if (match) {
      const end = index + [...match.source].length;
      output.push(match.replacement);
      substitutions.push({
        rule_id: match.id,
        source: input.slice(index, end).join(''),
        replacement: match.replacement,
        start: index,
        end,
      });
      index = end;
    } else {
      output.push(input[index]);
      index++;
    }
  }
  return { text: output.join(''), substitutions };
}
