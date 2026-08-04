"""Dialogue understanding and grounding (v2).

Two things that were tangled and are now separate:

    understanding   what this turn *means* — speaker, time, referent, ellipsis,
                    what is being asked. The model is good at this and Python
                    is not, so the model does it. None of it is authoritative.

    authority       what may be stated as fact. Rows, owned by the subsystems
                    that wrote them. Python decides, and the model's prose is
                    never evidence.

The failure that motivated the split: a reply cannot be checked without knowing
what it means, and meaning was being inferred from the draft alone. 「静かに
過ごしていました」 is unclassifiable until you know the question was 「今日は何
してた？」. So interpretation context flows in — and stops at the point where
something has to be *true*.
"""
