# Blender X Enfusion - Auto Collider Bounder

A Blender add-on for creating / converting mesh objects into clicks ready colliders for Enfusion Engine

## Features

- Create an automatic and self bounding collider(s) from the current selected object(s)
  - Box collider
  - Sphere collider
  - Capsule collider
  - Cylinder collider
  - Convex collider (from complex mesh)
  - Convex As Box: a box-shaped collider named as a convex one (e.g. `UCX_`)

- Convert any object(s) mesh(es) into a click ready collider(s) :
  - Convex collider
  - triangle collider

- Naming conventions you can create, edit and delete for your own pipeline
  - Built-in: `Unity / Enfusion` (UBX, USP, UCL, UCS, UCX, UTM) and `Unreal Engine`
  - Name pattern with `{type}` and `{name}` tokens, e.g. `{type}_{name}` → `UBX_Rock` or `{name}_{type}` → `Rock_boxcol`
  - Optional merge pattern with a `{target}` token, e.g. `{type}_{name}_mergecol_{target}`
  - Collider material per convention (picked by name, created with your color if it doesn't exist in the file)
  - `Update All Names` renames every collider in the file to the current convention

## Installation

1. Download the zip file from the latest release 
2. Open Blender
3. Go to `Edit` > `Preferences` > `Add-ons` > `drop-down arrow`
4. Click `Install...` and select the ZIP file you downloaded
5. Enable the addon by checking the box

## location 

`3D viewport` > `Sidebar (N)` > `Colliders` > `Collider Tools`

Naming conventions are edited in `Collider Tools` > `Naming Conventions` (or in the add-on preferences).
They are stored in Blender's preferences, so they are shared by all your .blend files.

## Usage

1. Select one or more objects in the 3D Viewport
2. press `Create Collider` if you wish to create a new object for the collider
3. for complex collision, I encoure you to create the object yourself,
    when you are ready to bake it, press `Convert To Collider`
4. Follow the dialog box, in case of doubt, hover the buttons to get a hint information
5. you're ready to export !


## Notes

- `Create Collider` creates a new object and never modifies the selected objects; only `Convert To Collider` changes them
- Convex and triangles colliders are based on the original mesh's geometry, so it is CRUCIAL to simplify the mesh beforehand

## License

MIT License
