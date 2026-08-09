"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { Bounds, Edges, GizmoHelper, GizmoViewport, Grid, Html, OrbitControls, useBounds } from "@react-three/drei";
import { Canvas, type ThreeEvent, useThree } from "@react-three/fiber";
import type { ManufacturingFeature, ResolvedPart } from "@/lib/design-types";

export type ViewMode = "perspective" | "front" | "side" | "top";

interface FurnitureViewerProps {
  parts: ResolvedPart[];
  designSize: { widthMm: number; heightMm: number; depthMm: number };
  selectedPartId?: string;
  viewMode: ViewMode;
  exploded: boolean;
  transparent: boolean;
  isolateSelection: boolean;
  onSelectPart: (partId?: string) => void;
}

function partSize(part: ResolvedPart): [number, number, number] {
  if (part.orientation === "YZ") {
    return [part.thickness_mm / 1_000, part.width_mm / 1_000, part.depth_mm / 1_000];
  }
  if (part.orientation === "XZ") {
    return [part.width_mm / 1_000, part.depth_mm / 1_000, part.thickness_mm / 1_000];
  }
  return [part.width_mm / 1_000, part.thickness_mm / 1_000, part.depth_mm / 1_000];
}

function explodedOffset(part: ResolvedPart, designSize: FurnitureViewerProps["designSize"]): [number, number, number] {
  const relativeX = part.position_mm.x - designSize.widthMm / 2;
  if (part.kind === "side") return [Math.sign(relativeX || 1) * 0.18, 0, 0];
  if (part.kind === "top") return [0, 0.2, 0];
  if (part.kind === "bottom") return [0, -0.12, 0];
  if (part.kind === "back") return [0, 0, 0.22];
  if (part.kind === "plinth") return [0, -0.06, -0.16];
  if (part.kind === "divider") return [Math.sign(relativeX || 1) * 0.08, 0, -0.08];
  const shelfNumber = Number(part.part_id.match(/shelf-(\d+)/)?.[1] ?? 0);
  return [0, 0, -0.04 * (shelfNumber % 3)];
}

function CameraRig({ viewMode, signature }: { viewMode: ViewMode; signature: string }) {
  const { camera } = useThree();
  const bounds = useBounds();

  useEffect(() => {
    camera.up.set(0, 1, 0);
    if (viewMode === "front") camera.position.set(0, 0, 6);
    if (viewMode === "side") camera.position.set(6, 0, 0);
    if (viewMode === "top") {
      camera.position.set(0, 6, 0.001);
      camera.up.set(0, 0, -1);
    }
    if (viewMode === "perspective") camera.position.set(2.7, 2.15, 2.7);
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
    const frame = requestAnimationFrame(() => bounds.refresh().clip().fit());
    return () => cancelAnimationFrame(frame);
  }, [bounds, camera, signature, viewMode]);

  return null;
}

function featureSurfaceTransform(
  part: ResolvedPart,
  feature: ManufacturingFeature,
): { position: [number, number, number]; rotation: [number, number, number] } | undefined {
  if (feature.face === "EDGE" || feature.x_mm === undefined || feature.y_mm === undefined) return undefined;
  const sign = feature.face === "A" ? 1 : -1;
  const epsilon = 0.0008;
  if (part.orientation === "XY") {
    return {
      position: [
        (feature.x_mm - part.width_mm / 2) / 1_000,
        sign * (part.thickness_mm / 2_000 + epsilon),
        (part.depth_mm / 2 - feature.y_mm) / 1_000,
      ],
      rotation: [-Math.PI / 2, 0, 0],
    };
  }
  if (part.orientation === "YZ") {
    return {
      position: [
        sign * (part.thickness_mm / 2_000 + epsilon),
        (feature.y_mm - part.width_mm / 2) / 1_000,
        (part.depth_mm / 2 - feature.x_mm) / 1_000,
      ],
      rotation: [0, Math.PI / 2, 0],
    };
  }
  return {
    position: [
      (feature.x_mm - part.width_mm / 2) / 1_000,
      (feature.y_mm - part.depth_mm / 2) / 1_000,
      sign * (part.thickness_mm / 2_000 + epsilon),
    ],
    rotation: [0, 0, 0],
  };
}

function FeatureMarker({ part, feature }: { part: ResolvedPart; feature: ManufacturingFeature }) {
  const transform = featureSurfaceTransform(part, feature);
  if (!transform || feature.kind === "outline" || feature.kind === "label") return null;
  const patternCount = Math.max(1, feature.pattern_count ?? 1);
  const pitch = (feature.pitch_mm ?? 0) / 1_000;
  if (feature.kind === "drill") {
    const diameter = Math.max(0.004, (feature.diameter_mm ?? feature.tool_diameter_mm ?? 5) / 1_000);
    return (
      <group position={transform.position} rotation={transform.rotation}>
        {Array.from({ length: patternCount }, (_, index) => (
          <mesh key={`${feature.id}-${index}`} position={[index * pitch, 0, 0]}>
            <ringGeometry args={[diameter * 0.32, diameter * 0.5, 20]} />
            <meshBasicMaterial color="#2563eb" depthTest={false} />
          </mesh>
        ))}
      </group>
    );
  }
  const width = Math.max(0.003, (feature.width_mm ?? 5) / 1_000);
  const length = Math.max(0.003, (feature.length_mm ?? feature.width_mm ?? 5) / 1_000);
  return (
    <mesh position={transform.position} rotation={transform.rotation}>
      <planeGeometry args={[width, length]} />
      <meshBasicMaterial color={feature.kind === "groove" ? "#b45309" : "#2563eb"} transparent opacity={0.6} depthTest={false} />
    </mesh>
  );
}

function PartMesh({
  part,
  designSize,
  selected,
  exploded,
  transparent,
  dimmed,
  onSelect,
}: {
  part: ResolvedPart;
  designSize: FurnitureViewerProps["designSize"];
  selected: boolean;
  exploded: boolean;
  transparent: boolean;
  dimmed: boolean;
  onSelect: (partId: string) => void;
}) {
  const [hovered, setHovered] = useState(false);
  const basePosition: [number, number, number] = [
    (part.position_mm.x - designSize.widthMm / 2) / 1_000,
    (part.position_mm.z - designSize.heightMm / 2) / 1_000,
    -(part.position_mm.y - designSize.depthMm / 2) / 1_000,
  ];
  const offset = exploded ? explodedOffset(part, designSize) : [0, 0, 0] as [number, number, number];
  const position: [number, number, number] = [
    basePosition[0] + offset[0],
    basePosition[1] + offset[1],
    basePosition[2] + offset[2],
  ];
  const opacity = dimmed ? 0.07 : transparent ? (selected ? 0.76 : 0.34) : 1;

  const handleSelect = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    onSelect(part.part_id);
  };

  return (
    <mesh
      position={position}
      castShadow={!transparent && !dimmed}
      receiveShadow
      onClick={handleSelect}
      onPointerOver={(event) => {
        event.stopPropagation();
        setHovered(true);
      }}
      onPointerOut={() => setHovered(false)}
    >
      <boxGeometry args={partSize(part)} />
      <meshStandardMaterial
        color={selected ? "#66e3aa" : hovered ? "#e2c79d" : part.color}
        roughness={0.72}
        metalness={0.02}
        transparent={opacity < 1}
        opacity={opacity}
        depthWrite={opacity > 0.25}
      />
      <Edges color={selected ? "#145c42" : dimmed ? "#94a39c" : "#574b3b"} threshold={18} />
      {selected ? part.features.map((feature) => <FeatureMarker key={feature.id} part={part} feature={feature} />) : null}
      {selected ? (
        <Html center position={[0, partSize(part)[1] / 2 + 0.06, 0]} distanceFactor={7}>
          <div className="canvas-part-label">{part.name}<small>{part.part_id}</small></div>
        </Html>
      ) : null}
    </mesh>
  );
}

function Scene(props: FurnitureViewerProps) {
  const signature = useMemo(
    () => props.parts.map((part) => `${part.part_id}:${part.width_mm}:${part.depth_mm}:${part.position_mm.x}:${part.position_mm.z}`).join("|"),
    [props.parts],
  );
  return (
    <>
      <color attach="background" args={["#eef0ec"]} />
      <ambientLight intensity={1.65} />
      <directionalLight position={[3, 5, 4]} intensity={2.2} castShadow shadow-mapSize={[1_024, 1_024]} />
      <directionalLight position={[-4, 1, -3]} intensity={0.7} color="#cadfd3" />
      <Bounds fit clip observe margin={1.35}>
        <group>
          {props.parts.map((part) => {
            const selected = part.part_id === props.selectedPartId;
            return (
              <PartMesh
                key={part.part_id}
                part={part}
                designSize={props.designSize}
                selected={selected}
                exploded={props.exploded}
                transparent={props.transparent}
                dimmed={props.isolateSelection && Boolean(props.selectedPartId) && !selected}
                onSelect={(partId) => props.onSelectPart(partId)}
              />
            );
          })}
        </group>
        <CameraRig viewMode={props.viewMode} signature={`${signature}:${props.exploded}`} />
      </Bounds>
      <Grid
        position={[0, -props.designSize.heightMm / 2_000 - 0.025, 0]}
        args={[8, 8]}
        cellSize={0.1}
        cellThickness={0.55}
        cellColor="#b7bdb8"
        sectionSize={0.5}
        sectionThickness={0.8}
        sectionColor="#8f9b94"
        fadeDistance={6}
        fadeStrength={1.5}
        infiniteGrid
      />
      <OrbitControls
        makeDefault
        enabled
        enableRotate={props.viewMode === "perspective"}
        minDistance={0.4}
        maxDistance={10}
        zoomSpeed={0.8}
        panSpeed={0.65}
      />
      <GizmoHelper alignment="bottom-right" margin={[62, 54]}>
        <GizmoViewport axisColors={["#c85757", "#39845f", "#4774ad"]} labelColor="#eef4f0" />
      </GizmoHelper>
    </>
  );
}

export default function FurnitureViewer(props: FurnitureViewerProps) {
  const orthographic = props.viewMode !== "perspective";
  return (
    <div className="canvas-shell" aria-label="Interaktiv 3D-modell av bokhyllan">
      <Canvas
        key={orthographic ? "orthographic" : "perspective"}
        orthographic={orthographic}
        camera={orthographic ? { near: 0.01, far: 100, zoom: 150 } : { near: 0.01, far: 100, fov: 38 }}
        dpr={[1, 1.75]}
        shadows
        gl={{ antialias: true, alpha: false, powerPreference: "high-performance" }}
        onPointerMissed={() => props.onSelectPart(undefined)}
      >
        <Suspense fallback={null}>
          <Scene {...props} />
        </Suspense>
      </Canvas>
      <div className="canvas-dimensions" aria-label="Aktuella yttermått">
        <span><small>X</small>{props.designSize.widthMm} mm</span>
        <span><small>Y</small>{props.designSize.depthMm} mm</span>
        <span><small>Z</small>{props.designSize.heightMm} mm</span>
      </div>
    </div>
  );
}
