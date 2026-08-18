import { localDesignHash } from "./design-engine";
import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
} from "./design-constraints";
import {
  DEFAULT_DESIGN_SPEC,
  type DesignSpec,
  type FurnitureType,
  type ReferenceImageConfirmedInputs,
  type ReferenceImageImport,
} from "./design-types";
/*
 * Pixel inference deliberately remains conservative. These are inference
 * limits, not product-envelope limits: reviewed controls can reach B1's
 * complete 40-shelf / 16-divider envelope.
 */
export const REFERENCE_IMAGE_INFERENCE_LIMITS = Object.freeze({
  shelfCount: 20,
  dividerCount: 12,
});

export interface PixelImage {
  width: number;
  height: number;
  data: ArrayLike<number>;
}

export interface ImageBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ReferenceImageAnalysis {
  imageWidth: number;
  imageHeight: number;
  bounds: ImageBounds;
  boundaryDetected: boolean;
  verticalGuides: number[];
  horizontalGuides: number[];
  baseGuides: number[];
  hasBaseCabinets: boolean;
  baseSplitRatio?: number;
  confidence: number;
  warnings: string[];
}

export interface ReferenceImageDraft {
  furnitureType: FurnitureType;
  widthMm: number;
  heightMm: number;
  depthMm: number;
  shelfCount: number;
  dividerCount: number;
  baseCabinetHeightMm: number;
  baseCabinetDepthMm: number;
  baseCabinetCount: number;
  bayWidthRatios: number[];
  shelfHeightRatios: number[];
}

export interface ReferenceImageResult {
  patch: Partial<DesignSpec>;
  metadata: ReferenceImageImport;
}

export interface ReferenceAssetInspection {
  import_id: string;
  project_id: string;
  image_sha256: string;
  media_type: string;
  size_bytes: number;
}

export const EMPTY_REFERENCE_CONFIRMATIONS: ReferenceImageConfirmedInputs = {
  dimensions_measured: false,
  layout_confirmed: false,
  material_confirmed: false,
  construction_assumptions_confirmed: false,
};

export function referenceVerificationFingerprint(spec: DesignSpec): string {
  const parametricSpec = { ...spec };
  delete parametricSpec.reference_image_import;
  return localDesignHash(parametricSpec);
}

export function referenceConfirmationsComplete(
  confirmations: ReferenceImageConfirmedInputs | undefined,
): boolean {
  return Boolean(
    confirmations?.dimensions_measured
    && confirmations.layout_confirmed
    && confirmations.material_confirmed
    && confirmations.construction_assumptions_confirmed,
  );
}

export function referenceImageVerificationIsCurrent(spec: DesignSpec): boolean {
  const provenance = spec.reference_image_import;
  return Boolean(
    provenance?.source === "reference_image"
    && /^[0-9a-f-]{36}$/.test(provenance.import_id)
    && /^[a-f0-9]{64}$/.test(provenance.image_sha256)
    && provenance.verification_status === "parametric_confirmed"
    && referenceConfirmationsComplete(provenance.confirmed_inputs)
    && provenance.verified_model_fingerprint === referenceVerificationFingerprint(spec),
  );
}

const clamp = (value: number, minimum: number, maximum: number) => (
  Number.isFinite(value) ? Math.min(maximum, Math.max(minimum, value)) : minimum
);
const luminance = (red: number, green: number, blue: number) => red * 0.2126 + green * 0.7152 + blue * 0.0722;
const RATIO_TOLERANCE = 1e-9;

function average(values: number[]): number {
  return values.length === 0 ? 0 : values.reduce((sum, value) => sum + value, 0) / values.length;
}

function standardDeviation(values: number[], mean: number): number {
  return Math.sqrt(average(values.map((value) => (value - mean) ** 2)));
}

function channelAt(image: PixelImage, x: number, y: number, channel: number): number {
  return Number(image.data[(y * image.width + x) * 4 + channel] ?? 0);
}

function sampledBackground(image: PixelImage): [number, number, number] {
  const sampleWidth = Math.max(2, Math.round(image.width * 0.06));
  const sampleHeight = Math.max(2, Math.round(image.height * 0.06));
  const values: [number[], number[], number[]] = [[], [], []];
  const origins: Array<readonly [number, number]> = [
    [0, 0],
    [image.width - sampleWidth, 0],
    [0, image.height - sampleHeight],
    [image.width - sampleWidth, image.height - sampleHeight],
  ];
  for (const [originX, originY] of origins) {
    for (let y = originY; y < originY + sampleHeight; y += 2) {
      for (let x = originX; x < originX + sampleWidth; x += 2) {
        values[0].push(channelAt(image, x, y, 0));
        values[1].push(channelAt(image, x, y, 1));
        values[2].push(channelAt(image, x, y, 2));
      }
    }
  }
  return values.map((channel) => average(channel)) as [number, number, number];
}

function colorDistance(image: PixelImage, x: number, y: number, color: [number, number, number]): number {
  return Math.sqrt(
    (channelAt(image, x, y, 0) - color[0]) ** 2
    + (channelAt(image, x, y, 1) - color[1]) ** 2
    + (channelAt(image, x, y, 2) - color[2]) ** 2,
  );
}

function longestSegment(flags: boolean[]): [number, number] | undefined {
  let bestStart = -1;
  let bestEnd = -1;
  let currentStart = -1;
  for (let index = 0; index <= flags.length; index += 1) {
    if (flags[index] && currentStart < 0) currentStart = index;
    if ((!flags[index] || index === flags.length) && currentStart >= 0) {
      const end = index - 1;
      if (end - currentStart > bestEnd - bestStart) {
        bestStart = currentStart;
        bestEnd = end;
      }
      currentStart = -1;
    }
  }
  return bestStart >= 0 ? [bestStart, bestEnd] : undefined;
}

function detectBounds(image: PixelImage): { bounds: ImageBounds; detected: boolean } {
  const background = sampledBackground(image);
  const columnCoverage = Array.from({ length: image.width }, () => 0);
  const rowCoverage = Array.from({ length: image.height }, () => 0);
  const step = 1;
  let sampledRows = 0;
  let sampledColumns = 0;

  for (let y = 0; y < image.height; y += step) {
    sampledRows += 1;
    for (let x = 0; x < image.width; x += step) {
      if (y === 0) sampledColumns += 1;
      if (colorDistance(image, x, y, background) < 42) continue;
      columnCoverage[x] = (columnCoverage[x] ?? 0) + 1;
      rowCoverage[y] = (rowCoverage[y] ?? 0) + 1;
    }
  }

  const columns = columnCoverage.map((coverage) => coverage / Math.max(1, sampledRows) > 0.09);
  const rows = rowCoverage.map((coverage) => coverage / Math.max(1, sampledColumns) > 0.09);
  const xSegment = longestSegment(columns);
  const ySegment = longestSegment(rows);
  if (!xSegment || !ySegment) {
    return {
      detected: false,
      bounds: {
        x: Math.round(image.width * 0.08),
        y: Math.round(image.height * 0.08),
        width: Math.round(image.width * 0.84),
        height: Math.round(image.height * 0.84),
      },
    };
  }
  const paddingX = Math.round(image.width * 0.012);
  const paddingY = Math.round(image.height * 0.012);
  const left = clamp(xSegment[0] - paddingX, 0, image.width - 2);
  const top = clamp(ySegment[0] - paddingY, 0, image.height - 2);
  const right = clamp(xSegment[1] + paddingX, left + 1, image.width - 1);
  const bottom = clamp(ySegment[1] + paddingY, top + 1, image.height - 1);
  return { detected: true, bounds: { x: left, y: top, width: right - left, height: bottom - top } };
}

function verticalProjection(image: PixelImage, bounds: ImageBounds, top: number, bottom: number): number[] {
  const scores = Array.from({ length: bounds.width }, () => 0);
  const startY = clamp(Math.round(top), bounds.y + 1, bounds.y + bounds.height - 2);
  const endY = clamp(Math.round(bottom), startY + 1, bounds.y + bounds.height - 1);
  for (let localX = 1; localX < bounds.width - 1; localX += 1) {
    const x = bounds.x + localX;
    let score = 0;
    for (let y = startY; y <= endY; y += 2) {
      score += Math.abs(
        luminance(channelAt(image, x + 1, y, 0), channelAt(image, x + 1, y, 1), channelAt(image, x + 1, y, 2))
        - luminance(channelAt(image, x - 1, y, 0), channelAt(image, x - 1, y, 1), channelAt(image, x - 1, y, 2)),
      );
    }
    scores[localX] = score / Math.max(1, (endY - startY) / 2);
  }
  return scores;
}

function horizontalProjection(image: PixelImage, bounds: ImageBounds, left: number, right: number): number[] {
  const scores = Array.from({ length: bounds.height }, () => 0);
  const startX = clamp(Math.round(left), bounds.x + 1, bounds.x + bounds.width - 2);
  const endX = clamp(Math.round(right), startX + 1, bounds.x + bounds.width - 1);
  for (let localY = 1; localY < bounds.height - 1; localY += 1) {
    const y = bounds.y + localY;
    let score = 0;
    for (let x = startX; x <= endX; x += 2) {
      score += Math.abs(
        luminance(channelAt(image, x, y + 1, 0), channelAt(image, x, y + 1, 1), channelAt(image, x, y + 1, 2))
        - luminance(channelAt(image, x, y - 1, 0), channelAt(image, x, y - 1, 1), channelAt(image, x, y - 1, 2)),
      );
    }
    scores[localY] = score / Math.max(1, (endX - startX) / 2);
  }
  return scores;
}

function smooth(values: number[]): number[] {
  return values.map((_, index) => average(values.slice(Math.max(0, index - 2), index + 3)));
}

function findGuideRatios(scores: number[], minimumRatio: number, maximumRatio: number, maximumCount = 16): number[] {
  const smoothed = smooth(scores);
  const start = Math.max(2, Math.round(smoothed.length * minimumRatio));
  const end = Math.min(smoothed.length - 3, Math.round(smoothed.length * maximumRatio));
  const range = smoothed.slice(start, end + 1);
  const mean = average(range);
  const threshold = mean + standardDeviation(range, mean) * 0.72;
  const candidates: { index: number; score: number }[] = [];
  for (let index = start + 1; index < end; index += 1) {
    if (smoothed[index]! < threshold) continue;
    if (smoothed[index]! < smoothed[index - 1]! || smoothed[index]! < smoothed[index + 1]!) continue;
    candidates.push({ index, score: smoothed[index]! });
  }
  const minimumGap = Math.max(5, Math.round(smoothed.length * 0.055));
  const selected: { index: number; score: number }[] = [];
  for (const candidate of candidates.sort((left, right) => right.score - left.score)) {
    if (selected.some((item) => Math.abs(item.index - candidate.index) < minimumGap)) continue;
    selected.push(candidate);
    if (selected.length >= maximumCount) break;
  }
  return selected
    .sort((left, right) => left.index - right.index)
    .map((item) => Number((item.index / Math.max(1, smoothed.length - 1)).toFixed(4)));
}

function averageBandColor(image: PixelImage, bounds: ImageBounds, yStart: number, yEnd: number): [number, number, number] {
  const values: [number[], number[], number[]] = [[], [], []];
  for (let y = clamp(Math.round(yStart), bounds.y, bounds.y + bounds.height - 1); y <= clamp(Math.round(yEnd), bounds.y, bounds.y + bounds.height - 1); y += 3) {
    for (let x = bounds.x + Math.round(bounds.width * 0.08); x < bounds.x + Math.round(bounds.width * 0.92); x += 5) {
      values[0].push(channelAt(image, x, y, 0));
      values[1].push(channelAt(image, x, y, 1));
      values[2].push(channelAt(image, x, y, 2));
    }
  }
  return values.map((channel) => average(channel)) as [number, number, number];
}

function rgbDistance(left: [number, number, number], right: [number, number, number]): number {
  return Math.sqrt((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 + (left[2] - right[2]) ** 2);
}

function detectBaseSplit(image: PixelImage, bounds: ImageBounds, scores: number[]): number | undefined {
  const candidates = findGuideRatios(scores, 0.52, 0.84, 6);
  const band = Math.max(3, bounds.height * 0.035);
  let best: { ratio: number; contrast: number } | undefined;
  for (const ratio of candidates) {
    const y = bounds.y + ratio * bounds.height;
    const above = averageBandColor(image, bounds, y - band * 2, y - band);
    const below = averageBandColor(image, bounds, y + band, y + band * 2);
    const contrast = rgbDistance(above, below);
    const aboveLight = luminance(...above);
    const belowLight = luminance(...below);
    if (contrast < 34 || belowLight > aboveLight + 12) continue;
    if (!best || contrast > best.contrast) best = { ratio, contrast };
  }
  return best?.ratio;
}

export function analyzeReferencePixels(image: PixelImage): ReferenceImageAnalysis {
  if (!Number.isInteger(image.width) || !Number.isInteger(image.height) || image.width < 160 || image.height < 160) {
    throw new Error("Bilden måste vara minst 160 × 160 pixlar.");
  }
  if (image.data.length < image.width * image.height * 4) throw new Error("Bildens pixeldata är ofullständig.");

  const detectedBounds = detectBounds(image);
  const { bounds } = detectedBounds;
  const horizontalScores = horizontalProjection(image, bounds, bounds.x, bounds.x + bounds.width);
  const baseSplitRatio = detectBaseSplit(image, bounds, horizontalScores);
  const openBottom = baseSplitRatio === undefined
    ? bounds.y + bounds.height
    : bounds.y + baseSplitRatio * bounds.height;
  const verticalGuides = findGuideRatios(
    verticalProjection(image, bounds, bounds.y, openBottom),
    0.055,
    0.945,
    REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount,
  );
  const horizontalGuides = findGuideRatios(
    horizontalScores,
    0.055,
    baseSplitRatio === undefined ? 0.945 : Math.max(0.1, baseSplitRatio - 0.045),
    REFERENCE_IMAGE_INFERENCE_LIMITS.shelfCount,
  ).filter((ratio) => baseSplitRatio === undefined || Math.abs(ratio - baseSplitRatio) > 0.065);
  const baseGuides = baseSplitRatio === undefined
    ? []
    : findGuideRatios(
        verticalProjection(image, bounds, bounds.y + baseSplitRatio * bounds.height, bounds.y + bounds.height),
        0.055,
        0.945,
        REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount,
      );

  const coverage = (bounds.width * bounds.height) / (image.width * image.height);
  const warnings: string[] = [];
  if (!detectedBounds.detected) warnings.push("Ingen tydlig möbelytterkant hittades. Välj en bild där hela möbeln syns mot bakgrunden.");
  if (coverage < 0.12) warnings.push("Möbeln upptar en liten del av bilden. Beskär gärna närmare.");
  if (verticalGuides.length === 0) warnings.push("Inga tydliga lodräta avdelare hittades.");
  if (horizontalGuides.length === 0) warnings.push("Inga tydliga hyllinjer hittades.");
  if (image.width < 480 || image.height < 360) warnings.push("En större bild ger säkrare linjedetektering.");
  const detectedStructure = Math.min(1, (verticalGuides.length + horizontalGuides.length) / 7);
  const confidence = Number(clamp(0.32 + Math.min(coverage, 0.5) * 0.55 + detectedStructure * 0.38 - warnings.length * 0.06 - (detectedBounds.detected ? 0 : 0.28), 0.08, 0.96).toFixed(2));

  return {
    imageWidth: image.width,
    imageHeight: image.height,
    bounds,
    boundaryDetected: detectedBounds.detected,
    verticalGuides,
    horizontalGuides,
    baseGuides,
    hasBaseCabinets: baseSplitRatio !== undefined,
    ...(baseSplitRatio === undefined ? {} : { baseSplitRatio }),
    confidence,
    warnings,
  };
}

function differences(guides: number[]): number[] {
  const boundaries = [0, ...guides, 1];
  return boundaries.slice(1).map((value, index) => Number((value - boundaries[index]!).toFixed(4)));
}

function equalBayRatios(count: number): number[] {
  if (count < 1 || 1 / count < 0.08 - RATIO_TOLERANCE) return [];
  return Array.from({ length: count }, () => 1 / count);
}

function sanitizeBayWidthRatios(ratios: number[], dividerCount: number): number[] {
  const count = dividerCount + 1;
  const fallback = equalBayRatios(count);
  if (ratios.length !== count || ratios.some((ratio) => !Number.isFinite(ratio) || ratio <= 0)) {
    return fallback;
  }
  const total = ratios.reduce((sum, ratio) => sum + ratio, 0);
  if (!Number.isFinite(total) || total <= 0) return fallback;
  const normalized = ratios.map((ratio) => ratio / total);
  return normalized.every((ratio) => ratio >= 0.08 - RATIO_TOLERANCE)
    ? normalized
    : fallback;
}

function equalShelfHeightRatios(count: number): number[] {
  if (count < 1) return [];
  const ratios = Array.from({ length: count }, (_, index) => (index + 1) / (count + 1));
  return ratios.every((ratio, index) => (
    ratio >= 0.05 - RATIO_TOLERANCE
    && ratio <= 0.95 + RATIO_TOLERANCE
    && (index === 0 || ratio - ratios[index - 1]! >= 0.05 - RATIO_TOLERANCE)
  )) ? ratios : [];
}

function sanitizeShelfHeightRatios(ratios: number[], shelfCount: number): number[] {
  const fallback = equalShelfHeightRatios(shelfCount);
  if (ratios.length !== shelfCount) return fallback;
  return ratios.every((ratio, index) => (
    Number.isFinite(ratio)
    && ratio >= 0.05 - RATIO_TOLERANCE
    && ratio <= 0.95 + RATIO_TOLERANCE
    && (index === 0 || ratio - ratios[index - 1]! >= 0.05 - RATIO_TOLERANCE)
  )) ? [...ratios] : fallback;
}

export function maximumReferenceBaseCabinetHeightMm(heightMm: number): number {
  return maximumBaseCabinetHeightMm(heightMm, DEFAULT_DESIGN_SPEC.measured_thickness_mm);
}

export function referenceDraftValidationError(draft: ReferenceImageDraft): string | undefined {
  if (draft.furnitureType !== "wall_library") return undefined;
  if (
    maximumReferenceBaseCabinetHeightMm(draft.heightMm)
    < DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm
  ) {
    return `Väggbibliotek kräver mer totalhöjd för ett underskåp på minst ${DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm} mm.`;
  }
  return undefined;
}

export function draftFromReferenceAnalysis(analysis: ReferenceImageAnalysis): ReferenceImageDraft {
  const aspect = analysis.bounds.width / Math.max(1, analysis.bounds.height);
  const lowFurniture = aspect >= 2.15;
  const heightMm = lowFurniture ? 1_200 : 2_400;
  const widthMm = clamp(Math.round((heightMm * aspect) / 50) * 50, 600, 6_000);
  const openRatio = analysis.baseSplitRatio ?? 1;
  const inferredHorizontalGuides = analysis.horizontalGuides
    .slice(0, REFERENCE_IMAGE_INFERENCE_LIMITS.shelfCount);
  const inferredVerticalGuides = analysis.verticalGuides
    .slice(0, REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount);
  const shelfHeightRatios = inferredHorizontalGuides
    .map((ratio) => 1 - ratio / Math.max(0.1, openRatio))
    .filter((ratio) => ratio > 0.04 && ratio < 0.96)
    .sort((left, right) => left - right)
    .map((ratio) => Number(ratio.toFixed(4)));
  const dividerCount = inferredVerticalGuides.length;
  const baseCount = analysis.baseGuides.length > 0
    ? Math.min(
        DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum,
        analysis.baseGuides.slice(0, REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount).length + 1,
      )
    : Math.max(1, dividerCount + 1);
  const maximumBaseHeightMm = maximumReferenceBaseCabinetHeightMm(heightMm);
  const baseHeight = analysis.baseSplitRatio === undefined
    ? 0
    : clamp(
        Math.round((heightMm * (1 - analysis.baseSplitRatio)) / 10) * 10,
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        maximumBaseHeightMm,
      );
  return {
    furnitureType: analysis.hasBaseCabinets ? "wall_library" : "bookcase",
    widthMm,
    heightMm,
    depthMm: analysis.hasBaseCabinets ? 520 : 320,
    shelfCount: shelfHeightRatios.length,
    dividerCount,
    baseCabinetHeightMm: baseHeight,
    baseCabinetDepthMm: analysis.hasBaseCabinets ? 520 : 0,
    baseCabinetCount: analysis.hasBaseCabinets ? baseCount : 0,
    bayWidthRatios: dividerCount > 0 ? differences(inferredVerticalGuides) : [1],
    shelfHeightRatios,
  };
}

export function referenceResult(
  analysis: ReferenceImageAnalysis,
  draft: ReferenceImageDraft,
  fileName: string,
  asset: ReferenceAssetInspection,
): ReferenceImageResult {
  const widthMm = clamp(
    Math.round(draft.widthMm),
    DESIGN_CONSTRAINTS.widthMm.minimum,
    DESIGN_CONSTRAINTS.widthMm.maximum,
  );
  const heightMm = clamp(
    Math.round(draft.heightMm),
    DESIGN_CONSTRAINTS.heightMm.minimum,
    DESIGN_CONSTRAINTS.heightMm.maximum,
  );
  const depthMm = clamp(
    Math.round(draft.depthMm),
    DESIGN_CONSTRAINTS.depthMm.minimum,
    DESIGN_CONSTRAINTS.depthMm.maximum,
  );
  const shelfCount = clamp(
    Math.round(draft.shelfCount),
    DESIGN_CONSTRAINTS.shelfCount.minimum,
    DESIGN_CONSTRAINTS.shelfCount.maximum,
  );
  const dividerCount = clamp(
    Math.round(draft.dividerCount),
    DESIGN_CONSTRAINTS.dividerCount.minimum,
    DESIGN_CONSTRAINTS.dividerCount.maximum,
  );
  const maximumBaseHeightMm = maximumReferenceBaseCabinetHeightMm(heightMm);
  if (
    draft.furnitureType === "wall_library"
    && maximumBaseHeightMm < DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm
  ) {
    throw new RangeError(
      `Väggbibliotek kräver mer totalhöjd för ett underskåp på minst ${DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm} mm.`,
    );
  }
  const bayWidthRatios = sanitizeBayWidthRatios(draft.bayWidthRatios, dividerCount);
  const shelfHeightRatios = sanitizeShelfHeightRatios(draft.shelfHeightRatios, shelfCount);
  const safeName = fileName.replace(/[\u0000-\u001f<>:"/\\|?*]/g, "_").slice(0, 120) || "referensbild";
  return {
    patch: {
      furniture_type: draft.furnitureType,
      width_mm: widthMm,
      height_mm: heightMm,
      depth_mm: depthMm,
      shelf_count: shelfCount,
      divider_count: dividerCount,
      bay_width_ratios: bayWidthRatios,
      shelf_height_ratios: shelfHeightRatios,
      base_cabinet_height_mm: draft.furnitureType === "wall_library"
        ? clamp(
            Math.round(draft.baseCabinetHeightMm),
            DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
            maximumBaseHeightMm,
          )
        : 0,
      base_cabinet_depth_mm: draft.furnitureType === "wall_library" ? depthMm : 0,
      base_cabinet_count: draft.furnitureType === "wall_library"
        ? clamp(Math.round(draft.baseCabinetCount), 1, DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum)
        : 0,
      reinforcement_mode: "manual",
      back_panel: true,
      plinth: true,
    },
    metadata: {
      source: "reference_image",
      import_id: asset.import_id,
      image_sha256: asset.image_sha256,
      file_name: safeName,
      image_width_px: analysis.imageWidth,
      image_height_px: analysis.imageHeight,
      confidence: analysis.confidence,
      detected_shelves: Math.min(analysis.horizontalGuides.length, REFERENCE_IMAGE_INFERENCE_LIMITS.shelfCount),
      detected_dividers: Math.min(analysis.verticalGuides.length, REFERENCE_IMAGE_INFERENCE_LIMITS.dividerCount),
      detected_base_cabinets: analysis.hasBaseCabinets,
      warnings: analysis.warnings,
      verification_status: "concept",
      confirmed_inputs: { ...EMPTY_REFERENCE_CONFIRMATIONS },
    },
  };
}
