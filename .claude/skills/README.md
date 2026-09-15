# Vendored skills

| Skill | Source | License | Used for |
|---|---|---|---|
| threejs-materials, -loaders, -renderers, -textures, -controls, -camera, -lights | https://github.com/full-stack-skills/threejs-skills | Apache-2.0 | `web/index.html` viewer (unlit baked materials, GLTFLoader, colour management, OrbitControls, preset cameras) |
| blender-web-pipeline | https://github.com/freshtechbro/claudedesignskills | MIT | `pipeline/blender_build.py` (bpy scene build, bake, decimate, glTF export for the web) |

External reference (not vendored, MIT): https://github.com/kajisho5/blender-skill — headless
inspect/convert/optimize/bake/validate CLIs for glTF budgets; useful as a second opinion on
`scene.glb` (`python scripts/check.py scene.glb --target webxr`).

Skills are user-invocable via the Skill tool once the repo is opened in Claude Code.
