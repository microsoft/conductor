import { beforeEach, describe, expect, it } from 'vitest';
import { claimCameraForAnimation, isCameraAnimating, releaseCamera } from './camera-authority';

beforeEach(() => {
  releaseCamera();
});

describe('camera authority', () => {
  it('reports no animation before anything claims the camera', () => {
    expect(isCameraAnimating(1000)).toBe(false);
  });

  it('holds the claim for the declared duration', () => {
    claimCameraForAnimation(300, 1000);
    expect(isCameraAnimating(1000)).toBe(true);
    expect(isCameraAnimating(1299)).toBe(true);
  });

  it('releases the claim once the duration elapses', () => {
    claimCameraForAnimation(300, 1000);
    expect(isCameraAnimating(1300)).toBe(false);
    expect(isCameraAnimating(5000)).toBe(false);
  });

  it('keeps the later deadline when claims overlap', () => {
    claimCameraForAnimation(400, 1000);
    // A shorter animation starting later must not shorten the outstanding claim.
    claimCameraForAnimation(100, 1200);
    expect(isCameraAnimating(1350)).toBe(true);
    expect(isCameraAnimating(1400)).toBe(false);
  });

  it('extends the deadline when a later claim outlasts the current one', () => {
    claimCameraForAnimation(100, 1000);
    claimCameraForAnimation(400, 1050);
    expect(isCameraAnimating(1400)).toBe(true);
    expect(isCameraAnimating(1450)).toBe(false);
  });

  it('drops an outstanding claim on release', () => {
    claimCameraForAnimation(300, 1000);
    releaseCamera();
    expect(isCameraAnimating(1000)).toBe(false);
  });
});
