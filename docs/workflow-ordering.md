# Workflow Ordering

Group `position` is an independent block-order coordinate, with ranks spaced by
1,000,000. Inserting after a
group uses its rank plus 1,000,000 and shifts following local groups by that
amount. Inherited ranks are shifted in the resolved child view, not in the parent.

Step `position` orders steps within a group. Overrides retain the inherited
position plus one per overriding layer. Group insertion and movement must not
rewrite step positions.

`step_anchor` is a separate step-space placement coordinate retained for each
group. Sorted anchors define block slots among ungrouped steps; group ranks fill
those slots. Thus a group reorder swaps grouped blocks without losing ungrouped
steps between slots. New empty groups need an explicit anchor; the editor supplies
one between adjacent sections. For API clients omitting an anchor, populated groups
fall back to their earliest step position, and empty groups fall back to zero.

Startup creates missing tables from the current models and does not rewrite
group or step positions.

Editor step moves use the displayed sequence: each group slot is one atomic,
fixed step-space boundary, and each inherited ungrouped step is another fixed
boundary. Local ungrouped runs receive distinct positions between those boundaries
without rounding. Adding an ungrouped step repairs local ties in the current form
and appends after the final displayed block, not after a grouped step coordinate.
If finite position space is exhausted, movement is refused rather than creating ties.

Writes reject equal effective ungrouped positions when a newly introduced local
step participates in the collision, including collisions with inherited ungrouped
steps. Overrides replace their inherited entry at base position plus one and are
not treated as new local steps. Equal positions in different groups remain valid.
This validation does not rewrite stored workflows.
