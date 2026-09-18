import { describe, expect, it } from 'vitest';
import { rebasedDraftFromBlocks } from './rebase';
import type { MergeBlock } from './types';

describe('rebasedDraftFromBlocks', () => {
  it('复现路径：保留远端非冲突改动，仅冲突区域填入本端文字', () => {
    // r0 = 第一行/第二行/第三行；B 一次保存同时改第二、三行（r1）；
    // A 从 r0 只改第二行 → 第二行冲突，第三行是 B 的非冲突改动。
    const blocks: MergeBlock[] = [
      { type: 'text', text: '第一行\n' },
      { type: 'conflict', base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' },
      { type: 'text', text: 'B第三行' },
    ];

    const draft = rebasedDraftFromBlocks(blocks, '');

    // 重基后的草稿：本端冲突文字 + 服务端非冲突的第三行（关键：不是 r0 的“第三行”）
    expect(draft).toBe('第一行\nA第二行\nB第三行');
    // 场记只整理冲突的第二行后，远端第三行仍在
    const resolved = draft.replace('A第二行', 'A+B第二行');
    expect(resolved).toBe('第一行\nA+B第二行\nB第三行');
  });

  it('无冲突脚手架缺失时退化为回退文本（优先服务端全文，绝不保留旧全文）', () => {
    expect(rebasedDraftFromBlocks(undefined, '服务端当前全文')).toBe('服务端当前全文');
    expect(rebasedDraftFromBlocks(null, '服务端当前全文')).toBe('服务端当前全文');
    expect(rebasedDraftFromBlocks([], '服务端当前全文')).toBe('服务端当前全文');
  });

  it('多个冲突块各自填入本端文字，干净文本逐字保留', () => {
    const blocks: MergeBlock[] = [
      { type: 'conflict', base: 'a\n', mine: 'A\n', theirs: 'B\n' },
      { type: 'text', text: '中间相同\n' },
      { type: 'conflict', base: 'c\n', mine: 'C\n', theirs: 'D\n' },
      { type: 'text', text: '结尾' },
    ];
    expect(rebasedDraftFromBlocks(blocks, '')).toBe('A\n中间相同\nC\n结尾');
  });

  it('干净块同时承载本端与远端的不相交改动（都会保留）', () => {
    // 中间区域撞车，两侧各自在不相交行的改动都落在干净块里。
    const blocks: MergeBlock[] = [
      { type: 'text', text: 'A改首行\n' },
      { type: 'conflict', base: '中\n', mine: '中A\n', theirs: '中B\n' },
      { type: 'text', text: 'B改末行\n' },
    ];
    expect(rebasedDraftFromBlocks(blocks, '')).toBe('A改首行\n中A\nB改末行\n');
  });
});
