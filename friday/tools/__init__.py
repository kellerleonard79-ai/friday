"""
tools/
Friday's tool layer: what the model may ask for, and what comes back.

Read the layer boundary before adding anything here. A tool computes and
returns; it does not message the user, does not touch Telegram, and does not
perform a side effect directly. Anything the world should see comes back as an
Effect on the ToolResult and is executed above, by the turn runner. That is
Phase III invariant 2, and it is the specific thing Phase II got wrong: tools
that sent messages mid-turn made permission cards arrive after the thing they
were meant to gate.

Step 3 is READ-ONLY. Effect is an empty base class with no executor behind it;
the effects layer is step 4.
"""
