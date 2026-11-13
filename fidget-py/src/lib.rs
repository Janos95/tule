//! Minimal Python bindings to Fidget for `tule.sdf`.
//!
//! Python builds an expression graph as a list of nodes `(op, a, b, value)`, where `a` and `b`
//! index earlier nodes (or are -1) and `value` is a constant's value. The last node is the
//! root. Each call rebuilds a Fidget `Context` from the list (cheap) and JIT-compiles it, so
//! nothing here holds state across threads. Arrays cross as little-endian `f32` bytes.

use fidget::context::{Context, Node};
use fidget::jit::JitShape;
use fidget::mesh::{Octree, Settings};
use fidget::render::ThreadPool;
use fidget::shape::EzShape;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

type Op = (String, i64, i64, f64);

fn err<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn build(ops: &[Op]) -> PyResult<(Context, Node)> {
    let mut ctx = Context::new();
    let mut nodes: Vec<Node> = Vec::with_capacity(ops.len());
    for (i, (op, a, b, v)) in ops.iter().enumerate() {
        let arg = |k: i64| -> PyResult<Node> {
            if k < 0 || k as usize >= i {
                return Err(PyValueError::new_err(format!("node {i} ({op}): bad argument {k}")));
            }
            Ok(nodes[k as usize])
        };
        let n = match op.as_str() {
            "x" => ctx.x(),
            "y" => ctx.y(),
            "z" => ctx.z(),
            "const" => ctx.constant(*v as f32),
            "neg" => ctx.neg(arg(*a)?).map_err(err)?,
            "abs" => ctx.abs(arg(*a)?).map_err(err)?,
            "sqrt" => ctx.sqrt(arg(*a)?).map_err(err)?,
            "square" => ctx.square(arg(*a)?).map_err(err)?,
            "recip" => ctx.recip(arg(*a)?).map_err(err)?,
            "sin" => ctx.sin(arg(*a)?).map_err(err)?,
            "cos" => ctx.cos(arg(*a)?).map_err(err)?,
            "tan" => ctx.tan(arg(*a)?).map_err(err)?,
            "asin" => ctx.asin(arg(*a)?).map_err(err)?,
            "acos" => ctx.acos(arg(*a)?).map_err(err)?,
            "atan" => ctx.atan(arg(*a)?).map_err(err)?,
            "exp" => ctx.exp(arg(*a)?).map_err(err)?,
            "ln" => ctx.ln(arg(*a)?).map_err(err)?,
            "floor" => ctx.floor(arg(*a)?).map_err(err)?,
            "ceil" => ctx.ceil(arg(*a)?).map_err(err)?,
            "round" => ctx.round(arg(*a)?).map_err(err)?,
            "add" => ctx.add(arg(*a)?, arg(*b)?).map_err(err)?,
            "sub" => ctx.sub(arg(*a)?, arg(*b)?).map_err(err)?,
            "mul" => ctx.mul(arg(*a)?, arg(*b)?).map_err(err)?,
            "div" => ctx.div(arg(*a)?, arg(*b)?).map_err(err)?,
            "min" => ctx.min(arg(*a)?, arg(*b)?).map_err(err)?,
            "max" => ctx.max(arg(*a)?, arg(*b)?).map_err(err)?,
            "atan2" => ctx.atan2(arg(*a)?, arg(*b)?).map_err(err)?,
            "mod" => ctx.modulo(arg(*a)?, arg(*b)?).map_err(err)?,
            _ => return Err(PyValueError::new_err(format!("unknown op {op:?}"))),
        };
        nodes.push(n);
    }
    let root = *nodes.last().ok_or_else(|| PyValueError::new_err("empty expression"))?;
    Ok((ctx, root))
}

fn floats(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

fn bytes_of(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|f| f.to_le_bytes()).collect()
}

const CHUNK: usize = 4096;

/// Values at points: `xyz` is n*3 f32 bytes; returns n f32 bytes (or n*4: value, d/dx, d/dy, d/dz).
#[pyfunction]
#[pyo3(signature = (ops, xyz, grad=false))]
fn eval<'py>(py: Python<'py>, ops: Vec<Op>, xyz: &[u8], grad: bool) -> PyResult<Bound<'py, PyBytes>> {
    let p = floats(xyz);
    if p.len() % 3 != 0 {
        return Err(PyValueError::new_err("xyz must hold n*3 floats"));
    }
    let out = py.detach(|| -> PyResult<Vec<f32>> {
        let (ctx, root) = build(&ops)?;
        let shape = JitShape::new(&ctx, root).map_err(err)?;
        let n = p.len() / 3;
        let mut out = Vec::with_capacity(if grad { 4 * n } else { n });
        let (mut xs, mut ys, mut zs) = (Vec::new(), Vec::new(), Vec::new());
        if grad {
            let tape = shape.ez_grad_slice_tape();
            let mut ev = JitShape::new_grad_slice_eval();
            for start in (0..n).step_by(CHUNK) {
                let end = (start + CHUNK).min(n);
                xs.clear(); ys.clear(); zs.clear();
                for i in start..end {
                    xs.push(fidget::types::Grad::new(p[3 * i], 1.0, 0.0, 0.0));
                    ys.push(fidget::types::Grad::new(p[3 * i + 1], 0.0, 1.0, 0.0));
                    zs.push(fidget::types::Grad::new(p[3 * i + 2], 0.0, 0.0, 1.0));
                }
                let r = ev.eval(&tape, &xs, &ys, &zs).map_err(err)?;
                for g in r {
                    out.extend_from_slice(&[g.v, g.dx, g.dy, g.dz]);
                }
            }
        } else {
            let tape = shape.ez_float_slice_tape();
            let mut ev = JitShape::new_float_slice_eval();
            let (mut xs, mut ys, mut zs): (Vec<f32>, Vec<f32>, Vec<f32>) = (Vec::new(), Vec::new(), Vec::new());
            for start in (0..n).step_by(CHUNK) {
                let end = (start + CHUNK).min(n);
                xs.clear(); ys.clear(); zs.clear();
                for i in start..end {
                    xs.push(p[3 * i]);
                    ys.push(p[3 * i + 1]);
                    zs.push(p[3 * i + 2]);
                }
                out.extend_from_slice(ev.eval(&tape, &xs, &ys, &zs).map_err(err)?);
            }
        }
        Ok(out)
    })?;
    Ok(PyBytes::new(py, &bytes_of(&out)))
}

/// Meshes the surface (value 0, inside negative) in the cube `center ± half`, at an octree of
/// `depth` levels (2^depth cells across), by Fidget's manifold dual contouring (sharp edges kept).
/// Returns (vertices: n*3 f32 bytes, triangles: m*3 u32 bytes).
#[pyfunction]
fn mesh<'py>(py: Python<'py>, ops: Vec<Op>, center: [f32; 3], half: f32, depth: u8)
    -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    if !(half > 0.0) || depth == 0 || depth > 12 {
        return Err(PyValueError::new_err("half > 0 and 1 <= depth <= 12"));
    }
    let (verts, tris) = py.detach(|| -> PyResult<(Vec<f32>, Vec<u8>)> {
        let (ctx, root) = build(&ops)?;
        let shape = JitShape::new(&ctx, root).map_err(err)?;
        let bound = shape.try_into().map_err(|_| PyValueError::new_err("the expression has variables"))?;
        // Fidget applies this to the octree's [-1, 1] cube to get model coordinates (despite its name)
        let world_to_model = nalgebra::Translation3::new(center[0], center[1], center[2]).to_homogeneous()
            * nalgebra::Matrix4::new_scaling(half);
        let settings = Settings { depth, world_to_model, threads: Some(&ThreadPool::Global), ..Default::default() };
        let octree = Octree::build(&bound, &settings).ok_or_else(|| PyValueError::new_err("meshing was cancelled"))?;
        let m = octree.walk_dual();
        let verts: Vec<f32> = m.vertices.iter().flat_map(|v| [v.x, v.y, v.z]).collect();
        let tris: Vec<u8> = m.triangles.iter()
            .flat_map(|t| [t.x as u32, t.y as u32, t.z as u32])
            .flat_map(|i| i.to_le_bytes())
            .collect();
        Ok((verts, tris))
    })?;
    Ok((PyBytes::new(py, &bytes_of(&verts)), PyBytes::new(py, &tris)))
}

#[pymodule]
fn tule_fidget(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(eval, m)?)?;
    m.add_function(wrap_pyfunction!(mesh, m)?)?;
    m.add("FIDGET_VERSION", "0.5.1")?;
    Ok(())
}
