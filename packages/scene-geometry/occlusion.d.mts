export function orderConvexFaces<T extends {points: number[][]}>(faces: T[]): {
  faces: T[];
  unresolved: number;
};
