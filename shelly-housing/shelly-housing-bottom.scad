// Bottom piece for Shelly EM housing
// This version includes access holes for:
// - Viewing status LEDs
// - Accessing control buttons
// - Allowing some ventilation
// Must be paired with shelly-housing-top.scad for complete assembly

// 0.5mm added to each dimension for clearance
// Shelly EM dimensions
sx = 39.5;
sy = 36.5;
sz = 17.5;

// Cutout dimensions
sc = 6;

// Wall width and lip height
ww = 2;
lh = 4;

$fn=30;

// Delta for subtraction
d = 0.1;

// Size of space for wires
wiredepth = 100;

module shelly() {
    difference() {
        cube([sx+ww*2, sy+ww*2, ww+lh]);
        translate([ww, ww, ww]) {
            cube([sx, sy, lh+d]);
        }
        translate([ww+sc, ww+sc, -d]) {
            cube([sx-sc*2, sy-sc*2, ww+d*2]);
        }
    }
}

module box() {
    shelly();
    translate([sx+ww, 0, 0]) {
        shelly();
    }
    // Left side
    cube([ww, sy+wiredepth, ww+sz/2]);
    // Bottom side
    cube([sx*2+ww*3, ww, ww+sz/2]);
    // Right side
    translate([(ww+sx)*2, 0, 0]) {
        cube([ww, sy+wiredepth, ww+sz/2]);
    }
    // Bottom fill
    translate([0, sy, 0]) {
        cube([sx*2+ww*3, wiredepth, ww]);
    }
    // Top side with holes
    translate([0, sy+wiredepth, 0]) {
        difference() {
            cube([sx*2+ww*3, ww, ww+sz/2]);
            // 9mm hole in middle
            // 4mm holes evenly spaced to left and right of middle
            for (hole=[1:5]) {
                diam = hole == 3 ? 9 : 4;
                translate([(sx*2+ww*3)*hole/6, -d, ww+sz/2]) {
                    rotate([270, 0, 0]) {
                        cylinder(h=ww+2*d, d=diam);
                    }
                }            
            }
        }
    }
}

difference() {
    union() {
        box();
        // Screw holes outside
        translate([2+ww, sy+25, 0]) {
            cylinder(h=ww+sz/2, d=4+ww*2);
        }
        translate([(ww+sx)*2-ww, sy+25, 0]) {
            cylinder(h=ww+sz/2, d=4+ww*2);
        }
        translate([2+ww, sy+wiredepth-25, 0]) {
            cylinder(h=ww+sz/2, d=4+ww*2);
        }
        translate([(ww+sx)*2-ww, sy+wiredepth-25, 0]) {
            cylinder(h=ww+sz/2, d=4+ww*2);
        }
    }
    // Screw holes
    translate([2+ww, sy+25, -d]) {
        cylinder(h=ww+sz/2+2*d, d=4);
    }
    translate([(ww+sx)*2-ww, sy+25, -d]) {
        cylinder(h=ww+sz/2+2*d, d=4);
    }
    translate([2+ww, sy+wiredepth-25, -d]) {
        cylinder(h=ww+sz/2+2*d, d=4);
    }
    translate([(ww+sx)*2-ww, sy+wiredepth-25, -d]) {
        cylinder(h=ww+sz/2+2*d, d=4);
    }
}
