"""Model training, labelling, validation and the artefact registry.

Importing this package must stay cheap: ``services/prediction_service.py``
imports ``models.labeling`` on every startup to learn the horizon set, and the
web app should not pay for scikit-learn at import time. So nothing is re-exported
here — import the submodule you need.
"""
