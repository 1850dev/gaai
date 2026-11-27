import { describe, expect, it } from 'vitest';
import { formatChangeRate, qualityMessages } from './quality';

describe('quality display', () => {
  it('renders flags even without notes, deduplicating explicit notes', () => {
    expect(qualityMessages({ sampled: true, notes: ['sampled'] })).toEqual([
      'サンプリングされた結果です。'
    ]);
    expect(
      qualityMessages({ subject_to_thresholding: true, data_loss_from_other_row: true })
    ).toHaveLength(2);
  });
  it('does not claim complete data when metadata is absent', () => {
    expect(qualityMessages()).toEqual([]);
    expect(qualityMessages({ notes: ['future_warning'] })).toEqual(['future_warning']);
  });
  it('explains the retrieved subset and approximate counts', () => {
    expect(
      qualityMessages({
        notes: ['result_row_limit_applied', 'unique_count_may_be_approximate']
      }).join(' ')
    ).toContain('全件ではありません');
  });
});

describe('change rate formatting', () => {
  it.each([
    [null, '—'],
    ['', '—'],
    ['NaN', '—'],
    ['Infinity', '—'],
    ['0', '0%'],
    ['-0.000001', '0%'],
    ['-0.287654321', '-28.77%'],
    ['1.25', '125%']
  ])('formats %s without inventing a baseline', (value, expected) => {
    expect(formatChangeRate(value)).toBe(expected);
  });
});
