# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from pathlib import Path

import numpy as np
import PIL.Image
import torch.nn as nn
import torch
import torchvision
from torchvision.models import resnet50
import torchvision.transforms as tr

import nevergrad as ng
import nevergrad.common.typing as tp
from .. import base
# pylint: disable=abstract-method


class Image(base.ExperimentFunction):
    def __init__(self, problem_name: str = "recovering", index: int = 0) -> None:
        """
        problem_name: the type of problem we are working on.
           recovering: we directly try to recover the target image.
        index: the index of the problem, inside the problem type.
           For example, if problem_name is "recovering" and index == 0,
           we try to recover the face of O. Teytaud.
        """

        # Storing high level information.
        self.domain_shape = (256, 256, 3)
        self.problem_name = problem_name
        self.index = index

        # Storing data necessary for the problem at hand.
        assert problem_name == "recovering"  # For the moment we have only this one.
        assert index == 0  # For the moment only 1 target.
        # path = os.path.dirname(__file__) + "/headrgb_olivier.png"
        path = Path(__file__).with_name("headrgb_olivier.png")
        image = PIL.Image.open(path).resize((self.domain_shape[0], self.domain_shape[1]), PIL.Image.ANTIALIAS)
        self.data = np.asarray(image)[:, :, :3]  # 4th Channel is pointless here, only 255.
        # parametrization
        array = ng.p.Array(init=128 * np.ones(self.domain_shape), mutable_sigma=True)
        array.set_mutation(sigma=35)
        array.set_bounds(lower=0, upper=255.99, method="clipping", full_range_sampling=True)
        max_size = ng.p.Scalar(lower=1, upper=200).set_integer_casting()
        array.set_recombination(ng.p.mutation.Crossover(axis=(0, 1), max_size=max_size)).set_name("")  # type: ignore

        super().__init__(self._loss, array)
        self.register_initialization(problem_name=problem_name, index=index)
        self._descriptors.update(problem_name=problem_name, index=index)

    def _loss(self, x: np.ndarray) -> float:
        assert self.problem_name == "recovering"
        x = np.array(x, copy=False).ravel()
        x = x.reshape(self.domain_shape)
        assert x.shape == self.domain_shape, f"Shape = {x.shape} vs {self.domain_shape}"
        # Define the loss, in case of recovering: the goal is to find the target image.
        assert self.index == 0
        value = float(np.sum(np.fabs(np.subtract(x, self.data))))
        return value


# #### Adversarial attacks ##### #


class Normalize(nn.Module):

    def __init__(self, mean: tp.ArrayLike, std: tp.ArrayLike) -> None:
        super().__init__()
        self.mean = torch.Tensor(mean)
        self.std = torch.Tensor(std)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return (x - self.mean.type_as(x)[None, :, None, None]) / self.std.type_as(x)[None, :, None, None]


class Resnet50(nn.Module):

    def __init__(self) -> None:
        super().__init__()
        self.norm = Normalize(mean=[0.485, 0.456, 0.406],
                              std=[0.229, 0.224, 0.225])
        self.model = resnet50(pretrained=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(self.norm(x))


class TestClassifier(nn.Module):

    def __init__(self, image_size: int = 224) -> None:
        super().__init__()
        self.model = nn.Linear(image_size * image_size * 3, 10)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(x.view(x.shape[0], -1))


# pylint: disable=too-many-arguments,too-many-instance-attributes
class ImageAdversarial(base.ExperimentFunction):

    def __init__(self, classifier: nn.Module, image: torch.Tensor, label: int = 0, targeted: bool = False,
                 epsilon: float = 0.05) -> None:
        # TODO add crossover params in args + criterion
        """
        params : needs to be detailed
        """
        self.targeted = targeted
        self.epsilon = epsilon
        self.image = image  # if (image is not None) else torch.rand((3, 224, 224))
        self.label = torch.Tensor([label])  # if (label is not None) else torch.Tensor([0])
        self.label = self.label.long()
        self.classifier = classifier  # if (classifier is not None) else Classifier()
        self.criterion = nn.CrossEntropyLoss()
        self.imsize = self.image.shape[1]

        array = ng.p.Array(init=np.zeros(self.image.shape), mutable_sigma=True, ).set_name("")
        array.set_mutation(sigma=self.epsilon / 10)
        array.set_bounds(lower=-self.epsilon, upper=self.epsilon, method="clipping", full_range_sampling=True)
        max_size = ng.p.Scalar(lower=1, upper=200).set_integer_casting()
        array.set_recombination(ng.p.mutation.Crossover(axis=(1, 2), max_size=max_size))  # type: ignore

        super().__init__(self._loss, array)
        self.register_initialization(classifier=classifier, image=image, label=label,
                                     targeted=targeted, epsilon=epsilon)
        # classifier and image cant be set as descriptors
        self.add_descriptors(label=label, targeted=targeted, epsilon=epsilon)

    @classmethod
    def _with_tag(
            cls,
            tags: tp.Dict[str, str],
            **kwargs: tp.Any,
    ) -> "ImageAdversarial":
        func = cls(**kwargs)
        func.add_descriptors(**tags)
        func._initialization_func = cls._with_tag  # type: ignore
        assert func._initialization_kwargs is not None
        func._initialization_kwargs["tags"] = tags
        return func

    def _loss(self, x: np.ndarray) -> float:
        x = torch.Tensor(x)
        image_adv = torch.clamp(self.image + x, 0, 1)
        image_adv = image_adv.view(1, 3, self.imsize, self.imsize)
        output_adv = self.classifier(image_adv)
        value = float(self.criterion(output_adv, self.label).item())
        return value * (1.0 if self.targeted else -1.0)

    @classmethod
    def make_benchmark_functions(
            cls,
            name: str,
    ) -> tp.Generator["ImageAdversarial", None, None]:
        tags = {"benchmark": name}
        if name == "test":
            imsize = 224
            classifier = TestClassifier(imsize)
            image = torch.rand((3, imsize, imsize))
            yield cls._with_tag(tags=tags, classifier=classifier, image=image,
                                label=0, targeted=False)
        elif name == "imagenet":
            classifier = Resnet50()
            imsize = 224
            transform = tr.Compose([tr.Resize(imsize), tr.CenterCrop(imsize), tr.ToTensor()])
            ifolder = torchvision.datasets.ImageFolder(data_folder, transform)
            data_loader = torch.utils.DataLoader(ifolder, batch_size=1, shuffle=True,
                                                 num_workers=8, pin_memory=True)
            for _, (data, target) in enumerate(data_loader):
                _, pred = torch.max(classifier(data), axis=1)
                if pred == target:
                    func = cls._with_tag(tags=tags, classifier=classifier, image=data[0],
                                         label=int(target), targeted=False, epsilon=0.05)
            yield func
        else:
            raise ValueError(f'Unknown benchmark case "{name}"')

    # @classmethod
#         x, y = torch.zeros(1, 3, 224, 224), 0
#         path_exist = True
#         data_loader = [(x, y)]
#         path_exist = False
