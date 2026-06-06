/**
 * Unit tests for SandboxWalkthrough.
 *
 * Covers the initStep() step-selection logic and basic component render gates.
 * initStep and LS_KEY are exported as test seams (see their export comments).
 *
 * Run with: cd web/app && npm test
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SandboxWalkthrough, initStep, LS_KEY } from './SandboxWalkthrough';

const NOOP = () => {};

// ── initStep() — pure step-selection logic ────────────────────────────────────

describe('initStep', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('clientCount=0 → done (no clients, nothing to tour)', () => {
    expect(initStep(0)).toBe('done');
  });

  it('clientCount=1 → welcome (first client just appeared)', () => {
    expect(initStep(1)).toBe('welcome');
  });

  it('clientCount=2 → loop (encourage adding more)', () => {
    expect(initStep(2)).toBe('loop');
  });

  it('clientCount=3 → done (auto-complete threshold)', () => {
    expect(initStep(3)).toBe('done');
  });

  it('clientCount>3 → done', () => {
    expect(initStep(4)).toBe('done');
    expect(initStep(10)).toBe('done');
  });

  it('LS_KEY=true → done regardless of clientCount', () => {
    localStorage.setItem(LS_KEY, 'true');
    expect(initStep(0)).toBe('done');
    expect(initStep(1)).toBe('done');
    expect(initStep(2)).toBe('done');
    expect(initStep(3)).toBe('done');
  });
});

// ── SandboxWalkthrough component render gates ─────────────────────────────────

describe('SandboxWalkthrough', () => {
  beforeEach(() => {
    localStorage.clear();
    // Freeze timers so the RAF loop inside useElemPos and
    // the fade-in / auto-advance timeouts do not fire during assertions.
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders null when clientCount=0 (step=done)', () => {
    const { container } = render(
      <SandboxWalkthrough
        clientCount={0}
        lastCapturedClientId={null}
        onOpenByLogin={NOOP}
        onOpenManual={NOOP}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders the skip button when clientCount=1 and LS_KEY unset (step=welcome)', () => {
    render(
      <SandboxWalkthrough
        clientCount={1}
        lastCapturedClientId={null}
        onOpenByLogin={NOOP}
        onOpenManual={NOOP}
      />,
    );
    // Skip button is always in DOM when step !== 'done'
    expect(screen.getByText('Skip walkthrough')).toBeDefined();
  });

  it('renders null when LS_KEY is already set to true', () => {
    localStorage.setItem(LS_KEY, 'true');
    const { container } = render(
      <SandboxWalkthrough
        clientCount={1}
        lastCapturedClientId={null}
        onOpenByLogin={NOOP}
        onOpenManual={NOOP}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when clientCount reaches 3 (auto-complete)', () => {
    const { container } = render(
      <SandboxWalkthrough
        clientCount={3}
        lastCapturedClientId={null}
        onOpenByLogin={NOOP}
        onOpenManual={NOOP}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
