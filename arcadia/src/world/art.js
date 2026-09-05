import * as THREE from 'three'

// Original, code-built miniature parts. Front faces +z; every root sits at y=0.
// The kit owns shared GPU resources: dispose the kit, not individual instances.
export function createArtKit() {
  const geometries = new Map()
  const materials = new Map()
  const palette = {
    plaster: '#eee1c4', stone: '#c5b99f', timber: '#695449', roof: '#be6549',
    roofDark: '#974f40', cream: '#f8ebcd', teal: '#477a77', navy: '#3c575f',
    window: '#ffd788', leaf: '#759665', leafLight: '#97ac73', pot: '#bb7654',
    water: '#77b8b3', brass: '#d4ad66', ink: '#34474a', paper: '#e7d6ad',
  }
  function geometry(key) {
    if (geometries.has(key)) return geometries.get(key)
    let result
    if (key === 'softbox') {
      const shape = new THREE.Shape()
      shape.moveTo(-0.43, -0.43)
      shape.lineTo(0.43, -0.43)
      shape.lineTo(0.43, 0.43)
      shape.lineTo(-0.43, 0.43)
      shape.closePath()
      result = new THREE.ExtrudeGeometry(shape, {
        depth: 0.86, bevelEnabled: true, bevelSegments: 1,
        steps: 1, bevelSize: 0.07, bevelThickness: 0.07,
      })
      result.center()
    } else if (key === 'gable' || key === 'sawtooth') {
      const shape = new THREE.Shape()
      shape.moveTo(-0.5, 0)
      shape.lineTo(key === 'sawtooth' ? 0.5 : 0, 0.5)
      shape.lineTo(0.5, 0)
      shape.closePath()
      result = new THREE.ExtrudeGeometry(shape, { depth: 1, bevelEnabled: false, steps: 1 })
      result.translate(0, 0, -0.5)
    } else if (key === 'sphere') result = new THREE.IcosahedronGeometry(0.5, 1)
    else if (key === 'cone') result = new THREE.ConeGeometry(0.5, 1, 8)
    else if (key === 'cylinder') result = new THREE.CylinderGeometry(0.5, 0.5, 1, 10)
    else if (key === 'pot') result = new THREE.CylinderGeometry(0.5, 0.36, 1, 8)
    else if (key === 'ring') result = new THREE.TorusGeometry(0.5, 0.055, 4, 20)
    else result = new THREE.BoxGeometry(1, 1, 1)
    geometries.set(key, result)
    return result
  }
  function material(color, glowing = false) {
    const hex = palette[color] || color
    const key = `${hex}:${glowing}`
    if (!materials.has(key)) materials.set(key, new THREE.MeshStandardMaterial({
      color: hex, roughness: 0.9, metalness: 0,
      ...(glowing ? { emissive: hex, emissiveIntensity: 0.12 } : {}),
    }))
    return materials.get(key)
  }
  function part(root, key, color, position, scale, rotation) {
    const mesh = new THREE.Mesh(geometry(key), material(color, color === 'window'))
    mesh.position.set(...position)
    mesh.scale.set(...scale)
    if (rotation) mesh.rotation.set(...rotation)
    mesh.castShadow = key !== 'ring'
    mesh.receiveShadow = true
    if (color === 'window') mesh.userData.window = true
    root.add(mesh)
    return mesh
  }
  const box = (root, color, position, scale) => part(root, 'softbox', color, position, scale)
  function window(root, x, y, z, w = 0.38, h = 0.48) {
    box(root, 'timber', [x, y, z], [w + 0.11, h + 0.1, 0.09])
    box(root, 'window', [x, y, z + 0.052], [w, h, 0.035])
    box(root, 'cream', [x, y - h / 2 - 0.04, z + 0.07], [w + 0.18, 0.07, 0.18])
  }
  function planter(root, x, z) {
    part(root, 'pot', 'pot', [x, 0.18, z], [0.3, 0.36, 0.3])
    part(root, 'sphere', 'leafLight', [x, 0.43, z], [0.4, 0.46, 0.4])
  }
  function building(model) {
    const root = new THREE.Group()
    root.name = `building:${model.id}`
    root.userData.buildingId = model.id
    const w = Number.isFinite(model.width) && model.width > 0 ? model.width : 2.6
    const d = Number.isFinite(model.depth) && model.depth > 0 ? model.depth : 2.4
    const kind = model.kind || 'home'
    const wallW = w * 0.8
    const wallD = d * 0.7
    const front = wallD / 2
    function fitted() {
      // Porches and pots belong to the plot too; keep paths clear at any plot size.
      const bounds = new THREE.Box3().setFromObject(root)
      const reachX = Math.max(Math.abs(bounds.min.x), Math.abs(bounds.max.x))
      const reachZ = Math.max(Math.abs(bounds.min.z), Math.abs(bounds.max.z))
      root.scale.x = Math.min(1, w / (reachX * 2))
      root.scale.z = Math.min(1, d / (reachZ * 2))
      return root
    }
    if (kind === 'square') {
      part(root, 'cylinder', 'stone', [0, 0.065, 0], [w * 0.94, 0.13, d * 0.94])
      part(root, 'cylinder', 'cream', [0, 0.22, 0], [w * 0.5, 0.32, d * 0.5])
      part(root, 'cylinder', 'water', [0, 0.39, 0], [w * 0.42, 0.035, d * 0.42])
      part(root, 'ring', 'stone', [0, 0.42, 0], [w * 0.46, d * 0.46, 1], [Math.PI / 2, 0, 0])
      part(root, 'cylinder', 'stone', [0, 0.65, 0], [0.3, 0.65, 0.3])
      part(root, 'pot', 'cream', [0, 1.02, 0], [0.72, 0.17, 0.72])
      part(root, 'sphere', 'brass', [0, 1.22, 0], [0.23, 0.35, 0.23])
      for (const side of [-1, 1]) {
        box(root, 'timber', [side * w * 0.34, 0.31, d * 0.28], [0.72, 0.13, 0.3])
        box(root, 'stone', [side * w * 0.34, 0.15, d * 0.28], [0.48, 0.3, 0.18])
      }
      return fitted()
    }
    if (kind === 'noticeboard') {
      box(root, 'stone', [0, 0.06, 0], [w * 0.75, 0.12, d * 0.55])
      for (const x of [-w * 0.24, w * 0.24]) box(root, 'timber', [x, 0.78, 0], [0.13, 1.5, 0.13])
      box(root, 'timber', [0, 1.12, 0], [w * 0.67, 0.87, 0.16])
      box(root, 'navy', [0, 1.12, 0.09], [w * 0.58, 0.7, 0.03])
      for (let i = 0; i < 3; i++) {
        const note = box(root, i === 1 ? 'brass' : 'paper', [(i - 1) * w * 0.17, 1.12 + (i % 2) * 0.08, 0.12], [w * 0.13, 0.34, 0.025])
        note.rotation.z = (i - 1) * 0.09
      }
      part(root, 'gable', 'roof', [0, 1.62, 0], [w * 0.8, 0.52, 0.65])
      planter(root, w * 0.34, 0.2)
      return fitted()
    }
    const tall = kind === 'archive'
    const lodge = kind === 'lodge'
    const workshop = kind === 'workshop'
    const home = kind === 'home'
    // Personal touches follow a resident's address, never their current project.
    // A finite palette preserves shared material batches across the whole village.
    const identity = [...String(model.id || '')].reduce((hash, c) => (Math.imul(hash, 31) + c.charCodeAt(0)) | 0, 0) >>> 0
    const variant = identity % 4
    const homeStyles = [
      { wall: '#eee1c4', roof: '#be6549', door: '#477a77' },
      { wall: '#e5cda9', roof: '#9f5141', door: '#5d748d' },
      { wall: '#d5ddc4', roof: '#9e714c', door: '#9b5846' },
      { wall: '#e3d8c9', roof: '#657d77', door: '#a47642' },
    ]
    const style = homeStyles[variant]
    const height = tall ? 2.25 : lodge ? 2.05 : workshop ? 2.2 : 1.35
    const color = workshop ? '#a3b3a0' : tall ? '#d8cfb4' : lodge ? '#dfc49e' : style.wall
    box(root, 'stone', [0, 0.1, 0], [wallW + 0.14, 0.2, wallD + 0.18])
    box(root, color, [0, height / 2 + 0.16, 0], [wallW, height, wallD])
    box(root, 'timber', [0, 0.27, front + 0.025], [wallW + 0.04, 0.11, 0.07])
    for (const x of [-wallW / 2 + 0.06, wallW / 2 - 0.06]) box(root, 'timber', [x, height / 2 + 0.13, front + 0.025], [0.09, height, 0.08])
    const roofY = height + 0.17
    const pitch = home ? 0.95 + variant * 0.1 : 1.15
    if (workshop) {
      // North-light roof bays identify the communal maker hall from any zoom.
      const bayWidth = (wallW + 0.36) / 3
      for (let bay = 0; bay < 3; bay++) {
        const x = (bay - 1) * bayWidth
        const roof = part(root, 'sawtooth', '#345b5a', [x, roofY, 0], [bayWidth, 1.25, wallD + 0.36])
        roof.userData.architecture = 'sawtooth-bay'
        const glazing = box(root, 'window', [x + bayWidth / 2 + 0.018, roofY + 0.35, 0], [0.035, 0.37, wallD * 0.83])
        glazing.userData.architecture = 'clerestory'
      }
    } else {
      part(root, 'gable', home ? style.roof : 'roof', [0, roofY, 0], [wallW + 0.36, pitch, wallD + 0.36])
      box(root, home ? style.roof : 'roofDark', [0, roofY + pitch / 2 + 0.01, 0], [0.15, 0.13, wallD + 0.4])
    }
    box(root, 'timber', [0, roofY - 0.035, front + 0.12], [wallW + 0.34, 0.1, 0.12])
    const doorX = workshop ? -wallW * 0.23 : 0
    box(root, 'timber', [doorX, workshop ? 0.81 : 0.66, front + 0.045], [workshop ? 1.05 : lodge ? 0.75 : 0.55, workshop ? 1.25 : 0.95, 0.11])
    box(root, workshop ? 'navy' : home ? style.door : 'teal', [doorX, workshop ? 0.8 : 0.65, front + 0.112], [workshop ? 0.94 : lodge ? 0.63 : 0.43, workshop ? 1.12 : 0.82, 0.045])
    part(root, 'sphere', 'brass', [doorX + 0.12, 0.64, front + 0.15], [0.045, 0.045, 0.045])
    box(root, 'stone', [doorX, 0.115, front + 0.27], [0.8, 0.16, 0.48])
    if (workshop) {
      window(root, wallW * 0.22, 1.01, front + 0.07, wallW * 0.32, 0.57)
      box(root, 'timber', [wallW * 0.21, 0.55, front + 0.3], [wallW * 0.42, 0.12, 0.36])
      part(root, 'cylinder', 'brass', [wallW * 0.2, 0.7, front + 0.3], [0.18, 0.18, 0.18])
      window(root, 0, 1.87, front + 0.07, wallW * 0.72, 0.32)
    } else {
      for (const x of [-wallW * 0.32, wallW * 0.32]) window(root, x, 0.96, front + 0.07, wallW * 0.16, 0.44)
    }
    if (tall || lodge) {
      box(root, 'timber', [0, 1.42, front + 0.04], [wallW, 0.1, 0.08])
      window(root, 0, tall ? 1.88 : 1.78, front + 0.075, 0.47, tall ? 0.54 : 0.33)
    }
    if (lodge) {
      const porch = box(root, 'timber', [0, 0.13, front + 0.49], [wallW * 0.96, 0.16, 1.1])
      porch.userData.architecture = 'communal-veranda'
      box(root, 'teal', [0, 1.46, front + 0.49], [wallW * 1.02, 0.14, 1.15]).rotation.x = 0.12
      for (const fraction of [-0.44, -0.18, 0.18, 0.44]) box(root, 'timber', [wallW * fraction, 0.77, front + 0.94], [0.09, 1.24, 0.09])
      for (const side of [-1, 1]) box(root, 'cream', [side * wallW * 0.31, 0.59, front + 0.94], [wallW * 0.26, 0.1, 0.09])
      box(root, 'timber', [-wallW * 0.32, 0.38, front + 0.34], [wallW * 0.26, 0.13, 0.37])
    } else {
      box(root, 'stone', [-wallW * 0.28, roofY + 0.43, -wallD * 0.2], [0.29, 0.82, 0.31])
      box(root, 'cream', [-wallW * 0.28, roofY + 0.84, -wallD * 0.2], [0.38, 0.12, 0.39])
    }
    const side = new THREE.Group()
    side.position.x = wallW / 2
    side.rotation.y = Math.PI / 2
    window(side, 0, tall ? 1.68 : 0.94, 0.04, Math.min(0.68, wallD * 0.4), tall ? 0.65 : 0.49)
    root.add(side)
    planter(root, wallW * 0.39, front + 0.35)
    if (home) {
      if (variant === 0) {
        box(root, 'pot', [-wallW * 0.32, 0.65, front + 0.2], [wallW * 0.22, 0.13, 0.24])
        for (const offset of [-0.12, 0.12]) part(root, 'sphere', '#d69d86', [-wallW * 0.32 + offset, 0.78, front + 0.2], [0.18, 0.19, 0.18])
      } else if (variant === 1) {
        part(root, 'gable', style.roof, [0, 1.18, front + 0.18], [0.92, 0.43, 0.58])
      } else if (variant === 2) {
        part(root, 'ring', 'cream', [0, roofY + 0.22, front + 0.19], [0.38, 0.38, 0.45])
        part(root, 'cylinder', 'window', [0, roofY + 0.22, front + 0.18], [0.31, 0.025, 0.31], [Math.PI / 2, 0, 0])
      } else {
        box(root, 'timber', [-wallW * 0.32, 0.28, front + 0.28], [0.7, 0.1, 0.32])
        box(root, 'stone', [-wallW * 0.32, 0.12, front + 0.28], [0.48, 0.24, 0.19])
      }
    }
    return fitted()
  }
  function agent(model) {
    const appearance = model.appearance || {}
    const body = appearance.body || '#628a85'
    const hat = appearance.hat || '#c8a164'
    const skin = appearance.skin || '#d6aa87'
    const variant = Math.abs(appearance.variant || 0) % 4
    const root = new THREE.Group()
    root.name = `agent:${model.id}`
    root.userData.agentId = model.id
    const legs = [-1, 1].map(side => {
      const pivot = new THREE.Group()
      pivot.position.set(side * 0.085, 0.3, 0)
      part(pivot, 'softbox', 'ink', [0, -0.12, 0.025], [0.125, 0.25, 0.17])
      root.add(pivot)
      return pivot
    })
    part(root, 'cone', body, [0, 0.4, 0], [0.37, 0.39, 0.3])
    const arms = [-1, 1].map(side => {
      const pivot = new THREE.Group()
      pivot.position.set(side * 0.18, 0.49, 0)
      part(pivot, 'softbox', body, [0, -0.09, 0], [0.105, 0.24, 0.12])
      pivot.rotation.z = side * 0.14
      root.add(pivot)
      return pivot
    })
    part(root, 'sphere', skin, [0, 0.66, 0.015], [0.31, 0.32, 0.29])
    // A tiny projecting nose establishes facing direction even at village scale.
    part(root, 'sphere', skin, [0, 0.66, 0.163], [0.07, 0.07, 0.075])
    if (variant === 0) {
      part(root, 'cylinder', hat, [0, 0.79, 0], [0.39, 0.045, 0.36])
      part(root, 'pot', hat, [0, 0.845, 0], [0.23, 0.11, 0.23])
    } else if (variant === 1) {
      part(root, 'sphere', hat, [0, 0.785, -0.025], [0.34, 0.2, 0.3])
      part(root, 'sphere', hat, [0, 0.895, -0.025], [0.09, 0.09, 0.09])
    } else if (variant === 2) {
      part(root, 'sphere', hat, [0, 0.76, -0.055], [0.33, 0.25, 0.29])
      part(root, 'sphere', hat, [0, 0.78, -0.21], [0.15, 0.17, 0.15])
    } else {
      part(root, 'cylinder', hat, [0, 0.785, 0], [0.34, 0.1, 0.3])
      box(root, hat, [0, 0.77, 0.14], [0.25, 0.045, 0.2])
    }
    box(root, 'timber', [0, 0.42, -0.18], [0.23, 0.27, 0.13])
    root.userData.legs = legs
    root.userData.arms = arms
    return root
  }
  function tree(seed = 0) {
    const root = new THREE.Group()
    const n = typeof seed === 'number' ? seed : [...String(seed)].reduce((sum, c) => sum + c.charCodeAt(0), 0)
    const size = 0.9 + (Math.abs(n) % 7) * 0.05
    part(root, 'cylinder', 'timber', [0, 0.58, 0], [0.17, 1.16, 0.17])
    if (n % 3 === 0) {
      part(root, 'cone', 'leaf', [0, 1.33, 0], [1.3, 1.55, 1.3])
      part(root, 'cone', 'leafLight', [0, 1.93, 0], [0.88, 1.2, 0.88])
    } else {
      part(root, 'sphere', 'leaf', [0, 1.52, 0], [1.38, 1.5, 1.2])
      part(root, 'sphere', 'leafLight', [-0.29, 1.7, 0.19], [0.9, 1.0, 0.92])
    }
    root.scale.setScalar(size)
    root.rotation.y = n * 2.39996
    return root
  }
  return {
    building, agent, tree,
    dispose() {
      geometries.forEach(value => value.dispose())
      materials.forEach(value => value.dispose())
      geometries.clear()
      materials.clear()
    },
  }
}
