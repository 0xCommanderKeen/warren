import { describe, expect, it } from 'vitest';
import { daylightAt } from './daylight.js';
describe('local village lighting', () => {
  const at = (hour, minute=0) => daylightAt(new Date(2026,8,5,hour,minute));
  it('uses real local time and keeps nighttime distinct from noon', () => {
    expect(at(12).daylight).toBe(1);
    expect(at(0).night).toBe(true);
    expect(at(12).phase).toBe('Daylight');
  });
  it('interpolates across sunrise and midnight without brightness jumps', () => {
    expect(Math.abs(at(6).daylight-at(5,59).daylight)).toBeLessThan(.01);
    expect(Math.abs(at(0).daylight-at(23,59).daylight)).toBeLessThan(.01);
  });
});
