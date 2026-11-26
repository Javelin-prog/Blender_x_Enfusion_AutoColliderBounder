bl_info = {
    "name": "[JavStud] Auto Collider Bounder",
    "author": "'AntiPoney' Jérôme Noël",
    "version": (2, 2, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Colliders",
    "description": "Automatically add colliders to selected objects with customizable naming conventions (Unity / Enfusion, Unreal or your own).",
    "category": "Object",
}

import bpy
import os

def register():
    from . import AutoColliderBounder
    AutoColliderBounder.register()

def unregister():
    from . import AutoColliderBounder
    AutoColliderBounder.unregister()

if __name__ == "__main__":
    register()
