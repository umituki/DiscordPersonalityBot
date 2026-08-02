"""Admin control plane (spec 30).

Kept free of re-exports so importing the models never drags the control plane
(and therefore the memory engine) into a module that only needs a risk class.
"""
