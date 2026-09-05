const samePoint = (a, b) => a[0] === b[0] && a[1] === b[1];

export function createMotion(destination) {
  return { position: [...destination], destination: [...destination], points: [], step: 0 };
}

export function retargetMotion(motion, model, paused = false) {
  if (paused) {
    motion.position = [...model.destination];
    motion.destination = [...model.destination];
    motion.points = [];
    motion.step = 0;
    return;
  }
  if (samePoint(motion.destination, model.destination)) return;

  // Finish the existing street route before joining the new route at its origin.
  // Cutting directly to that origin during travel can cross occupied plots.
  const remaining = motion.points.slice(motion.step);
  const points = [...remaining, ...model.route, model.destination];
  let previous = motion.position;
  motion.points = points.filter(point => {
    if (samePoint(previous, point)) return false;
    previous = point;
    return true;
  }).map(point => [...point]);
  motion.destination = [...model.destination];
  motion.step = 0;
}

export function advanceMotion(motion, dt, speed = 2.1) {
  let distanceLeft = Number.isFinite(dt) && Number.isFinite(speed) ? Math.max(0, dt) * Math.max(0, speed) : 0;
  let heading = 0;
  let moved = false;
  while (motion.step < motion.points.length) {
    const point = motion.points[motion.step];
    const dx = point[0] - motion.position[0];
    const dz = point[1] - motion.position[1];
    const distance = Math.hypot(dx, dz);
    if (distance === 0) { motion.step += 1; continue; }
    if (distanceLeft <= 0) break;
    heading = Math.atan2(dx, dz);
    moved = true;
    if (distance <= distanceLeft) {
      motion.position = [...point];
      motion.step += 1;
      distanceLeft -= distance;
    } else {
      motion.position[0] += dx / distance * distanceLeft;
      motion.position[1] += dz / distance * distanceLeft;
      distanceLeft = 0;
    }
  }
  if (motion.step === motion.points.length) {
    motion.points = [];
    motion.step = 0;
  }
  return { walking: moved && motion.points.length > 0, heading };
}
